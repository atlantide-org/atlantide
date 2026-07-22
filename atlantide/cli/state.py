"""``atlantide state`` — administer the state backend itself.

Three commands, all about the store rather than the resources in it: ``check``
verifies the backend is reachable and safely configured, ``migrate`` copies state
between the local database and the remote backend, and ``unlock`` shows and
breaks leases a dead run left behind.

They share one shape: resolve the target, open exactly one backend, close it.
:class:`~atlantide.cli.target.StateTarget` supplies the first part; ``closing``
the last.
"""

from __future__ import annotations

import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape
from rich.table import Table

from atlantide.cli.console import console
from atlantide.cli.errors import fail
from atlantide.cli.options import ConfirmOpt, require_confirm
from atlantide.cli.target import StateTarget, default_state, load_project
from atlantide.core import AtlantideError
from atlantide.core.check import FAIL, OK, SKIP, WARN, Check, Status
from atlantide.state import SqliteStateBackend
from atlantide.state.backend import Lease, StateBackend

app = typer.Typer(help="Inspect, move, and unblock engine state.")

#: How each status renders; the colour carries severity at a glance.
_MARK: dict[Status, str] = {
    OK: "[green]ok  [/]",
    WARN: "[yellow]warn[/]",
    FAIL: "[red]fail[/]",
    SKIP: "[dim]--  [/]",
}


def _announced_target() -> StateTarget:
    """This invocation's state target, having said which one it is."""
    target = StateTarget.resolve(None, load_project())
    target.announce()
    return target


# -- check --------------------------------------------------------------------


@app.command("check")
def check(
    probe: Annotated[
        bool,
        typer.Option(
            "--probe/--no-probe",
            help="Also verify conditional writes by writing to a scratch key.",
        ),
    ] = True,
) -> None:
    """Verify the configured state backend is reachable and safely set up.

    The bucket, the lock table and their settings are the trust root for shared
    state, and atlantide deliberately does not create them. Nothing else verifies
    them either: versioning left off or a missing lock-table TTL costs nothing
    until the day it costs everything. This reports all of it at once, rather
    than letting an apply discover it one failed call at a time.
    """
    target = _announced_target()
    with closing(target.open()) as backend:
        checks = backend.check()
        if probe:
            checks.append(backend.probe())
    checks.append(_secrets_check(target))
    for result in checks:
        # Escaped: details quote config keys such as [state].backend.
        console.print(f"{_MARK[result.status]} {result.name}: {escape(result.detail)}")
    if any(result.failed for result in checks):
        raise typer.Exit(1)


def _secrets_check(target: StateTarget) -> Check:
    """The configured provider's own verdict on whether it can serve a secret.

    Building the registry is itself part of what is being checked — an unknown
    AWS profile or an unreadable keyfile fails here rather than in ``check()`` —
    so both steps are guarded. A doctor command reports what is wrong; it does
    not become one more thing that crashes.
    """
    name = target.project.secrets.provider
    try:
        return target.secrets().get(name).check()
    except AtlantideError as exc:
        return Check(f"secrets: {name}", FAIL, str(exc))
    except Exception as exc:
        # Broad on purpose: a provider's SDK raises on its own terms (botocore
        # ProfileNotFound, an OS error on the keyfile) and none of it should
        # abort the report the operator asked for.
        return Check(f"secrets: {name}", FAIL, f"{type(exc).__name__}: {exc}")


# -- migrate ------------------------------------------------------------------


@app.command("migrate")
def migrate(
    source: Annotated[
        Path | None, typer.Option("--from", help="Local state database to copy from.")
    ] = None,
    to_local: Annotated[
        Path | None,
        typer.Option(
            "--to-local",
            help="Reverse direction: copy the remote backend into this local database.",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Overwrite a destination that already holds state."),
    ] = False,
    confirm: ConfirmOpt = False,
) -> None:
    """Copy state between the local database and the remote backend.

    Adopting a backend runs one way by default (local -> the ``[state]`` table);
    ``--to-local`` runs the other, so leaving a shared backend is as easy as
    joining one. Either direction refuses a destination that already holds nodes
    unless ``--force`` is given: merging two states is a decision, not a guess.
    """
    project = load_project()
    if not project.state_backend.is_remote:
        fail("no remote backend configured — set [state].backend in atlantide.toml")
    remote = StateTarget.resolve(None, project)
    copy = (
        _adopt_local(remote, to_local)
        if to_local is not None
        else _adopt_remote(remote, source if source is not None else default_state(project))
    )
    _run(copy, force=force, confirm=confirm)


@dataclass(frozen=True, slots=True)
class _Copy:
    """One direction of a migration: two open backends and what to say afterwards."""

    source: StateBackend
    source_label: str
    destination: StateBackend
    destination_label: str
    #: What the operator still has to do once the bytes have moved.
    epilogue: str


def _adopt_remote(remote: StateTarget, source: Path) -> _Copy:
    if not source.is_file():
        fail(f"no local state database at {source}")
    return _Copy(
        source=SqliteStateBackend(str(source)),
        source_label=str(source),
        destination=remote.open(),
        destination_label=remote.label,
        epilogue=(
            f"{source} is no longer read — keep it as a backup or remove it, "
            f"but do not keep applying against both"
        ),
    )


def _adopt_local(remote: StateTarget, destination: Path) -> _Copy:
    return _Copy(
        source=remote.open(),
        source_label=remote.label,
        destination=SqliteStateBackend(str(destination)),
        destination_label=str(destination),
        epilogue=(
            f"remove the [state] table from atlantide.toml (or pass "
            f"--state {destination}) for commands to use it"
        ),
    )


def _run(copy: _Copy, *, force: bool, confirm: bool) -> None:
    """Move a whole state across, in whichever direction ``copy`` describes.

    One write (``put_many``), not a loop: a migration interrupted halfway would
    leave a destination that is neither empty — so a retry refuses it — nor
    complete, so an apply would recreate live resources.
    """
    with closing(copy.source), closing(copy.destination):
        graph, outputs = copy.source.load(), copy.source.outputs()
        existing = len(copy.destination.load())
        if existing and not force:
            fail(
                f"{copy.destination_label} already holds {existing} node(s) — refusing "
                f"to overwrite it. Pass --force to replace it, or point at an empty "
                f"destination"
            )
        require_confirm(
            confirm,
            f"copy {len(graph)} node(s) from {copy.source_label} to "
            f"{copy.destination_label}"
            + (f", replacing {existing} node(s) there" if existing else "")
            + "?",
        )
        copy.destination.put_many(graph.nodes.values())
        if outputs:
            copy.destination.set_outputs(outputs)
    console.print(
        f"[green]migrated[/] {len(graph)} node(s) to {escape(copy.destination_label)}\n"
        f"[dim]{escape(copy.epilogue)}[/]"
    )


# -- unlock -------------------------------------------------------------------


@app.command("unlock")
def unlock(
    node: Annotated[
        list[str] | None,
        typer.Option("--node", help="Break the hold on this node id (repeatable)."),
    ] = None,
    owner: Annotated[
        str | None, typer.Option("--owner", help="Break every hold held by this owner.")
    ] = None,
    every: Annotated[
        bool, typer.Option("--all", help="Break every hold recorded in the backend.")
    ] = False,
    confirm: ConfirmOpt = False,
) -> None:
    """Show who holds the state lock, and break a hold left behind by a dead run.

    A lease outlives the run that took it: a killed CI job blocks its teammates
    until the TTL lapses, with nothing to do but wait. With no selector this only
    lists the holds — breaking one while its run is alive lets two applies write
    the same resources, so it names the holder and asks first.
    """
    target = _announced_target()
    with closing(target.open()) as backend:
        held = backend.locks()
        if not held:
            console.print("[dim]no locks held[/]")
            return
        _render_locks(held)
        if not (node or owner or every):
            console.print(
                "\n[dim]pass --node/--owner/--all to break a hold "
                "(only when you know the run is gone)[/]"
            )
            return
        targets = _selected(held, node, owner, every)
        require_confirm(confirm, f"\nBreak {len(targets)} lock(s)?")
        broken = backend.force_unlock(targets)
    console.print(f"[green]unlocked[/] {broken} node(s)")


def _render_locks(held: dict[str, Lease]) -> None:
    now = time.time()
    table = Table(title="State locks")
    table.add_column("node", style="bold")
    table.add_column("owner")
    table.add_column("expires in")
    for node_id in sorted(held):
        lease = held[node_id]
        remaining = lease.expires_at - now
        table.add_row(
            node_id, lease.owner, f"{remaining:.0f}s" if remaining > 0 else "[dim]expired[/]"
        )
    console.print(table)


def _selected(
    held: dict[str, Lease], nodes: list[str] | None, owner: str | None, every: bool
) -> set[str]:
    """The node ids the selectors name. A selector that matches nothing is an error,
    so a typo cannot read as "broke everything you asked for"."""
    if every:
        return set(held)
    selected: set[str] = set()
    if owner is not None:
        selected |= {nid for nid, lease in held.items() if lease.owner == owner}
        if not selected:
            fail(f"no locks held by {owner!r}")
    if nodes:
        unknown = sorted(set(nodes) - set(held))
        if unknown:
            fail(f"not locked: {', '.join(unknown)}")
        selected |= set(nodes)
    return selected
