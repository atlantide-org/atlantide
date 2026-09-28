"""``atlantide state migrate`` — copy state between the local database and the remote backend."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack, closing
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, NamedTuple

import typer
from rich.markup import escape

from atlantide.cli.commands.state.common import replace_contents
from atlantide.cli.console import console
from atlantide.cli.errors import fail
from atlantide.cli.options import ConfirmOpt, require_confirm
from atlantide.cli.target import StateTarget, current_project, default_state
from atlantide.engine.locking import held_lock
from atlantide.state import LockPolicy, SqliteStateBackend, StateBackend, StateGraph
from atlantide.util.fs import create_private

__all__ = ["migrate"]

FromOpt = Annotated[Path | None, typer.Option("--from", help="Local state database to copy from.")]
ToLocalOpt = Annotated[
    Path | None,
    typer.Option(
        "--to-local",
        help="Reverse direction: copy the remote backend into this local database.",
    ),
]
MigrateForceOpt = Annotated[
    bool,
    typer.Option("--force", help="Overwrite a destination that already holds state."),
]


def migrate(
    source: FromOpt = None,
    to_local: ToLocalOpt = None,
    force: MigrateForceOpt = False,
    confirm: ConfirmOpt = False,
) -> None:
    """Copy state between the local database and the remote backend.

    By default copies local state to the ``\\[state]`` backend; ``--to-local``
    copies the other way. Either direction refuses a destination that already
    holds nodes unless ``--force`` is given, rather than guessing how to merge.
    """
    project = current_project()
    if not project.state_backend.is_remote:
        fail("no remote backend configured — set [state].backend in atlantide.toml")
    remote = StateTarget.resolve(None, project)
    copy = (
        _adopt_local(remote, to_local)
        if to_local is not None
        else _adopt_remote(remote, source if source is not None else default_state(project))
    )
    _run(copy, force=force, confirm=confirm, policy=remote.lock_policy)


@dataclass(frozen=True, slots=True)
class _Copy:
    """One direction of a migration: two open backends and what to say afterwards."""

    source: StateBackend
    source_label: str
    destination: StateBackend
    destination_label: str
    #: Follow-up steps printed after the copy.
    epilogue: str


def _adopt_remote(remote: StateTarget, source: Path) -> _Copy:
    if not source.is_file():
        fail(f"no local state database at {source}")
    local, opened = _open_both(lambda: SqliteStateBackend(str(source)), remote.open)
    return _Copy(
        source=local,
        source_label=str(source),
        destination=opened,
        destination_label=remote.label,
        epilogue=(
            f"{source} is no longer read — keep it as a backup or remove it, "
            f"but do not keep applying against both"
        ),
    )


def _adopt_local(remote: StateTarget, destination: Path) -> _Copy:
    # Pre-create owner-only: sqlite otherwise creates the database under the
    # process umask (typically world-readable), and it receives a copy of the
    # whole remote state.
    try:
        create_private(destination, nofollow=True)
    except OSError as exc:
        fail(f"cannot create local state database {destination}: {exc}")
    opened, local = _open_both(remote.open, lambda: SqliteStateBackend(str(destination)))
    return _Copy(
        source=opened,
        source_label=remote.label,
        destination=local,
        destination_label=str(destination),
        epilogue=(
            f"remove the [state] table from atlantide.toml (or pass "
            f"--state {destination}) for commands to use it"
        ),
    )


def _open_both(
    first: Callable[[], StateBackend], second: Callable[[], StateBackend]
) -> tuple[StateBackend, StateBackend]:
    """Open two backends, closing the first if the second cannot be opened."""
    opened = first()
    try:
        return opened, second()
    except BaseException:
        opened.close()
        raise


def _run(copy: _Copy, *, force: bool, confirm: bool, policy: LockPolicy) -> None:
    """Move a whole state across, in whichever direction ``copy`` describes.

    One write (``replace_many``), not a loop: an interrupted migration would
    leave a destination that is neither empty (so a retry refuses it) nor
    complete (so an apply would recreate live resources).

    Both sides are locked for the copy, so an apply against the source cannot
    produce a torn copy. The lock scope comes from a read taken before the locks,
    so the source's serial is also compared across the write.

    The locks are acquired in order of label, so two migrations running in
    opposite directions cannot deadlock.
    """
    with closing(copy.source), closing(copy.destination):
        graph = copy.source.load()
        existing = _refuse_occupied(copy, force=force)
        _confirm_copy(copy, graph, existing, confirm=confirm)
        sides = _lock_order(copy, frozenset(graph.nodes))
        graph = _copy_under_lock(copy, sides, policy)
    console.print(
        f"[green]migrated[/] {len(graph)} node(s) to {escape(copy.destination_label)}\n"
        f"[dim]{escape(copy.epilogue)}[/]"
    )


class _Side(NamedTuple):
    """One backend to lock, under the label that orders it and the scope it needs."""

    label: str
    backend: StateBackend
    scope: frozenset[str]


def _refuse_occupied(copy: _Copy, *, force: bool) -> int:
    """Return the destination's node count; fail if it is non-empty without ``force``."""
    existing = len(copy.destination.load())
    if existing and not force:
        fail(
            f"{copy.destination_label} already holds {existing} node(s) — refusing "
            f"to overwrite it. Pass --force to replace it, or point at an empty "
            f"destination"
        )
    return existing


def _confirm_copy(copy: _Copy, graph: StateGraph, existing: int, *, confirm: bool) -> None:
    require_confirm(
        f"copy {len(graph)} node(s) from {copy.source_label} to "
        f"{copy.destination_label}"
        + (f", replacing {existing} node(s) there" if existing else "")
        + "?",
        confirm=confirm,
    )


def _lock_order(copy: _Copy, incoming: frozenset[str]) -> list[_Side]:
    """Both sides, sorted by label.

    The destination's scope must cover the nodes about to *arrive*, not only
    those already there: an empty destination would lock nothing and then be
    written outside its own lease.
    """
    return sorted(
        [
            _Side(copy.source_label, copy.source, incoming),
            _Side(
                copy.destination_label,
                copy.destination,
                incoming | frozenset(copy.destination.load().nodes),
            ),
        ],
        key=lambda side: side.label,
    )


def _copy_under_lock(copy: _Copy, sides: list[_Side], policy: LockPolicy) -> StateGraph:
    """Re-read the source under both locks and replace the destination with it."""
    with ExitStack() as locks:
        for side in sides:
            locks.enter_context(held_lock(side.backend, side.scope, policy=policy))
        before = copy.source.serial()
        graph, outputs = copy.source.load(), copy.source.outputs()
        replace_contents(copy.destination, graph.nodes, outputs)
        if copy.source.serial() != before:
            fail(
                f"{copy.source_label} changed while it was being copied — the "
                f"destination may be incomplete. Re-run the migration"
            )
    return graph
