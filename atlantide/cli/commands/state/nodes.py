"""``atlantide state list`` / ``show`` / ``rm`` — what state contains.

Reads take no lock: a locked read would block on a wedged apply, and a torn
listing is harmless because nothing acts on it.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
from typing import Annotated, Any

import typer
from rich.markup import escape

from atlantide.cli.commands.state.common import (
    announced_target,
    quiet_target,
    snapshot_document,
)
from atlantide.cli.commands.state.snapshot import default_snapshot, write_snapshot
from atlantide.cli.console import console
from atlantide.cli.errors import fail
from atlantide.cli.options import ConfirmOpt, JsonOpt, StateOpt, require_confirm
from atlantide.cli.target import StateTarget
from atlantide.cli.views.common import SECRET_REDACTED
from atlantide.cli.views.output import emit_json
from atlantide.cli.views.state import node_json, node_list_json, render_node, render_nodes
from atlantide.core.errors import StateError
from atlantide.engine.locking import held_lock, require_no_new_nodes
from atlantide.secrets import is_sealed_marker
from atlantide.state import StateBackend, StateGraph, StateNode
from atlantide.state.codec import encode

__all__ = ["list_nodes", "remove_nodes", "show_node"]

ShowNodeIdArg = Annotated[str, typer.Argument(help="Node id, as `state list` prints it.")]
RevealOpt = Annotated[
    bool,
    typer.Option("--reveal", "-r", help="Print sealed output values in the clear."),
]
RemoveNodeIdsArg = Annotated[list[str], typer.Argument(help="Node ids to forget.")]
RemoveForceOpt = Annotated[
    bool,
    typer.Option("--force", help="Also forget nodes marked prevent_destroy."),
]
BackupOpt = Annotated[
    bool,
    typer.Option("--backup/--no-backup", help="Snapshot state first (default: yes)."),
]


def list_nodes(
    state: StateOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """List the resources state records.

    A row marked DRIFTED has no usable input hash, so the next plan cannot skip
    it. ``refresh --write`` sets it when it sees live drift, and a failed rollback
    sets it too.
    """
    target = quiet_target(state) if json_out else announced_target(state)
    with closing(target.open()) as backend:
        nodes = backend.load().nodes
    if json_out:
        emit_json(node_list_json(target.label, nodes))
        return
    render_nodes(nodes)


def show_node(
    node_id: ShowNodeIdArg,
    state: StateOpt = None,
    json_out: JsonOpt = False,
    reveal: RevealOpt = False,
) -> None:
    """Print everything state records about one resource.

    Sensitive outputs are sealed at rest and stay redacted without ``--reveal``,
    as with ``secret get``: the output lands in terminal scrollback and CI logs.
    """
    target = quiet_target(state) if json_out else announced_target(state)
    with closing(target.open()) as backend:
        node = backend.load().get(node_id)
        if node is None:
            fail(f"no node {node_id!r} in state — `atlantide state list` shows what is there")
        outputs = _shown_outputs(node, target, reveal=reveal)
    if json_out:
        emit_json(node_json(target.label, node, outputs))
        return
    render_node(node, outputs)


def _shown_outputs(node: StateNode, target: StateTarget, *, reveal: bool) -> dict[str, Any]:
    """The node's outputs: unsealed with ``reveal``, sealed values redacted otherwise."""
    if not reveal:
        return {
            key: SECRET_REDACTED if is_sealed_marker(value) else value
            for key, value in node.outputs.items()
        }
    secrets = target.secrets()
    return {key: secrets.unseal(value) for key, value in node.outputs.items()}


def remove_nodes(
    node_ids: RemoveNodeIdsArg,
    state: StateOpt = None,
    force: RemoveForceOpt = False,
    backup: BackupOpt = True,
    confirm: ConfirmOpt = False,
) -> None:
    """Forget a resource without destroying it.

    Use this for a state row that no longer describes a real resource, such as one
    deleted out of band or left behind by a failed rollback.

    It does not touch the provider. If the resource still exists, atlantide stops
    tracking it and the next apply tries to create a second one; use
    `atlantide destroy` to remove the resource itself.
    """
    unique = sorted(set(node_ids))
    target = announced_target(state)
    with closing(target.open()) as backend:
        # Preview read without the lock, so a wedged apply does not block the prompt.
        preview = backend.load()
        _check_removable(preview.nodes, unique, force=force)
        _render_forget(preview.nodes, unique)
        require_confirm(f"\nForget {len(unique)} node(s)?", confirm=confirm)
        # The backup covers all of state, so it locks all of state, as `state backup`
        # does: table-shaped backends read node by node, and a concurrent write
        # would tear the snapshot.
        scope = frozenset(preview.nodes) if backup else frozenset(unique)
        with held_lock(backend, scope, policy=target.lock_policy):
            # Act on state as read under the lease, not as previewed: an apply that
            # wrote while this waited is in the backup, and the checks re-run.
            graph = backend.load()
            if backup:
                try:
                    require_no_new_nodes(
                        graph, scope, "rm", "nothing was removed; re-run rm to review them"
                    )
                except StateError as exc:
                    fail(str(exc))
            _check_removable(graph.nodes, unique, force=force)
            if backup:
                _backup_before_forget(backend, graph)
            # One unit where the backend has transactions: an interruption does not
            # leave some of the ids forgotten and the rest not.
            backend.replace_many(unique, ())
    console.print(f"[green]forgot[/] {len(unique)} node(s)")


def _check_removable(nodes: Mapping[str, StateNode], unique: list[str], *, force: bool) -> None:
    """Refuse ids state does not hold, and protected ones without ``--force``."""
    if missing := sorted(set(unique) - set(nodes)):
        fail(f"not in state: {', '.join(missing)}")
    protected = [nid for nid in unique if nodes[nid].prevent_destroy]
    if protected and not force:
        fail(f"{', '.join(protected)} declare prevent_destroy — pass --force to forget them anyway")


def _render_forget(nodes: Mapping[str, StateNode], unique: list[str]) -> None:
    """Print the nodes ``rm`` will forget, and that their resources stay live."""
    for node_id in unique:
        console.print(f"  [red]- forget[/] {escape(node_id)} [dim]({nodes[node_id].type})[/]")
    console.print(
        "\n[yellow]The underlying resources are not destroyed.[/] They stay live "
        "and untracked, and the next apply will try to create them again."
    )


def _backup_before_forget(backend: StateBackend, graph: StateGraph) -> None:
    """Snapshot ``graph`` into the working directory before any delete.

    Call under the lock, with ``graph`` read under it.
    """
    doc = snapshot_document(backend, graph)
    snapshot = default_snapshot(doc.serial)
    write_snapshot(snapshot, encode(doc), overwrite=False)
    console.print(f"[dim]backed up to {escape(str(snapshot))}[/]")
