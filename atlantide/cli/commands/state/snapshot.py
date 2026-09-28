"""``atlantide state backup`` / ``restore`` — snapshot state and put a snapshot back.

Snapshots use the S3 backend's document encoding (canonical JSON, gzipped past a
threshold, self-describing and version-checked on read), so state has a single
serialized format to keep compatible.
"""

from __future__ import annotations

import time
from contextlib import closing
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from atlantide.cli.commands.state.common import (
    announced_target,
    replace_contents,
    snapshot_document,
)
from atlantide.cli.console import console
from atlantide.cli.errors import fail
from atlantide.cli.options import ConfirmOpt, StateOpt, require_confirm
from atlantide.core.errors import StateError
from atlantide.engine.locking import held_lock, require_no_new_nodes
from atlantide.state.codec import StateDocument, decode, encode
from atlantide.util.fs import write_private

__all__ = ["SNAPSHOT_SUFFIX", "backup", "default_snapshot", "restore", "write_snapshot"]

#: Extension for a state snapshot. Distinct from `.atlas` (a compiled config)
#: because both are content-addressed blobs.
SNAPSHOT_SUFFIX = ".atlas-state"

BackupPathArg = Annotated[
    Path | None,
    typer.Argument(
        help=f"Where to write the snapshot (default: ./atlantide-state-*{SNAPSHOT_SUFFIX})."
    ),
]
BackupForceOpt = Annotated[
    bool, typer.Option("--force", help="Overwrite an existing file at that path.")
]
RestorePathArg = Annotated[Path, typer.Argument(help="Snapshot written by `state backup`.")]
RestoreForceOpt = Annotated[
    bool,
    typer.Option(
        "--force",
        help="Restore even though state has changed since the snapshot was taken.",
    ),
]


def write_snapshot(path: Path, data: bytes, *, overwrite: bool) -> None:
    """Write ``data`` to ``path`` owner-only, never through a planted symlink.

    Snapshots hold every recorded resource attribute, including identifiers,
    outputs and sealed secrets. Without ``overwrite`` an existing path (or a
    symlink planted there) is refused; see :func:`~atlantide.util.fs.write_private`.
    """
    try:
        write_private(path, data, overwrite=overwrite)
    except FileExistsError:
        fail(f"{path} already exists — pass --force to overwrite it")
    except OSError as exc:
        fail(f"cannot write snapshot {path}: {exc}")


def default_snapshot(serial: int) -> Path:
    """A snapshot file name carrying its serial and the UTC time it was taken."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return Path(f"atlantide-state-{serial}-{stamp}{SNAPSHOT_SUFFIX}")


def backup(
    path: BackupPathArg = None,
    state: StateOpt = None,
    force: BackupForceOpt = False,
) -> None:
    """Write the whole of state (nodes, outputs, serial) to one file.

    Take one before anything that rewrites state in bulk: an upgrade that
    migrates the schema, a `state restore`, a `--force` migration. Recovery
    otherwise depends on the store's own history, which the local database does
    not keep and S3 keeps only if bucket versioning is enabled.

    Runs under the state lock because table-shaped backends read node by node: a
    snapshot taken during an apply could capture only some of its writes.
    """
    target = announced_target(state)
    with closing(target.open()) as backend:
        scope = frozenset(backend.load().nodes)
        with held_lock(backend, scope, policy=target.lock_policy):
            graph = backend.load()
            # The scope predates the lease: a node created while this waited is
            # not covered, and an apply writing it could tear the snapshot.
            try:
                require_no_new_nodes(graph, scope, "backup", "re-run backup")
            except StateError as exc:
                fail(str(exc))
            doc = snapshot_document(backend, graph)
        destination = path if path is not None else default_snapshot(doc.serial)
        write_snapshot(destination, encode(doc), overwrite=force)
    console.print(
        f"[green]backed up[/] {len(doc.nodes)} node(s) at serial {doc.serial} "
        f"to {escape(str(destination))}"
    )


def restore(
    path: RestorePathArg,
    state: StateOpt = None,
    force: RestoreForceOpt = False,
    confirm: ConfirmOpt = False,
) -> None:
    """Replace the contents of state with a snapshot.

    This does not touch any cloud resource; it rewrites atlantide's record of
    them. Restoring an old snapshot therefore *creates* drift rather than
    undoing it: resources created after the snapshot become untracked, and the
    next apply will try to create them again. Use it to undo a bad state write,
    not a bad deployment.

    Refuses when state has moved since the snapshot was taken unless --force,
    since the two may then disagree about live resources.
    """
    if not path.is_file():
        fail(f"no snapshot at {path}")
    doc = _read_snapshot(path)
    target = announced_target(state)
    with closing(target.open()) as backend:
        # Preview read without the lock, so a wedged apply does not block the prompt.
        previewed_serial = _check_restorable(backend.serial(), doc, force=force)
        current = backend.load()
        obsolete = sorted(set(current.nodes) - set(doc.nodes))
        added = sorted(set(doc.nodes) - set(current.nodes))
        _render_restore(doc, added, obsolete)
        require_confirm(
            f"\nReplace state with {len(doc.nodes)} node(s) from {path}?", confirm=confirm
        )
        scope = frozenset(current.nodes) | frozenset(doc.nodes)
        with held_lock(backend, scope, policy=target.lock_policy):
            # A write between the preview and the lock makes the serial check stale,
            # and a node it created is outside the locked scope.
            if backend.serial() != previewed_serial:
                fail(
                    "state was written to while restore waited for the lock — "
                    "nothing was restored; re-run restore to review the new changes"
                )
            replace_contents(backend, doc.nodes, doc.outputs)
    console.print(f"[green]restored[/] {len(doc.nodes)} node(s) from {escape(str(path))}")


def _check_restorable(current_serial: int, doc: StateDocument, *, force: bool) -> int:
    """Return ``current_serial``; fail if it differs from the snapshot's, unless ``force``."""
    if current_serial != doc.serial and not force:
        fail(
            f"state is at serial {current_serial} but this snapshot was taken at "
            f"{doc.serial} — it has been written to since. Pass --force to replace "
            f"it anyway, after checking what changed"
        )
    return current_serial


def _read_snapshot(path: Path) -> StateDocument:
    """Decode a snapshot, reporting a bad file as a diagnostic rather than a trace."""
    try:
        return decode(path.read_bytes())
    except (StateError, OSError) as exc:
        fail(f"cannot read snapshot {path}: {exc}")


def _render_restore(doc: StateDocument, added: list[str], obsolete: list[str]) -> None:
    """Print the nodes the restore adds or removes, before asking.

    Nodes present in both are overwritten with the snapshot's copy and are not listed.
    """
    console.print(f"restoring {len(doc.nodes)} node(s) taken at serial {doc.serial}")
    for node_id in added:
        console.print(f"  [green]+ restored[/] {escape(node_id)}")
    for node_id in obsolete:
        console.print(
            f"  [red]- forgotten[/] {escape(node_id)} "
            f"[dim](not in the snapshot; the resource is not destroyed)[/]"
        )
