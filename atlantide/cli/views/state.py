"""What the ``state`` subcommands show: rows, one node, locks and the journal check."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypedDict

from rich.markup import escape
from rich.table import Table

from atlantide.cli.console import console
from atlantide.state import (
    NO_INPUT_HASH,
    Lease,
    NodeStatus,
    StateNode,
)

if TYPE_CHECKING:
    # Type-only: the S3 backend imports boto3, which `--help` does not need.
    from atlantide.state.s3 import FsckReport

__all__ = [
    "NodeJson",
    "NodeListJson",
    "NodeRowJson",
    "node_json",
    "node_list_json",
    "render_fsck",
    "render_locks",
    "render_node",
    "render_nodes",
]


class NodeRowJson(TypedDict):
    """One row of ``state list --json``."""

    node_id: str
    type: str
    provider: str
    status: str
    drifted: bool
    depends_on: list[str]


class NodeListJson(TypedDict):
    """The ``state list --json`` document."""

    state: str
    nodes: list[NodeRowJson]


class NodeJson(TypedDict):
    """The ``state show --json`` document."""

    state: str
    node_id: str
    type: str
    provider: str
    provider_version: str
    status: str
    drifted: bool
    prevent_destroy: bool
    depends_on: list[str]
    properties: dict[str, Any]
    outputs: dict[str, Any]


# -- list ---------------------------------------------------------------------


def render_nodes(nodes: Mapping[str, StateNode]) -> None:
    """One row per recorded node, sorted by id; says so when state is empty."""
    if not nodes:
        console.print("[dim]state is empty[/]")
        return
    table = Table(title=f"{len(nodes)} resource(s)")
    table.add_column("node", style="bold")
    table.add_column("type")
    table.add_column("status")
    for node_id, node in sorted(nodes.items()):
        table.add_row(node_id, node.type, _status_of(node))
    console.print(table)


def node_list_json(label: str, nodes: Mapping[str, StateNode]) -> NodeListJson:
    return {
        "state": label,
        "nodes": [
            {
                "node_id": node.id,
                "type": node.type,
                "provider": node.provider,
                "status": node.status,
                "drifted": node.input_hash == NO_INPUT_HASH,
                "depends_on": list(node.dependencies),
            }
            for _, node in sorted(nodes.items())
        ],
    }


def _status_of(node: StateNode) -> str:
    """The row's status markup: DRIFTED first, then any status other than ``created``."""
    if node.input_hash == NO_INPUT_HASH:
        return "[red]DRIFTED[/]"
    if node.status != NodeStatus.CREATED:
        return f"[yellow]{node.status}[/]"
    return "[dim]created[/]"


# -- show ---------------------------------------------------------------------


def render_node(node: StateNode, outputs: Mapping[str, Any]) -> None:
    """Everything state records about ``node``; ``outputs`` already redacted or revealed."""
    console.print(f"[bold]{escape(node.id)}[/]")
    console.print(f"  type       {escape(node.type)}")
    console.print(f"  provider   {escape(node.provider)} {escape(node.provider_version)}")
    console.print(f"  status     {_status_of(node)}")
    if node.prevent_destroy:
        console.print("  [yellow]prevent_destroy[/]")
    if node.dependencies:
        console.print(f"  depends on {escape(', '.join(node.dependencies))}")
    _print_mapping("Inputs", node.properties)
    _print_mapping("Outputs", outputs)


def node_json(label: str, node: StateNode, outputs: dict[str, Any]) -> NodeJson:
    return {
        "state": label,
        "node_id": node.id,
        "type": node.type,
        "provider": node.provider,
        "provider_version": node.provider_version,
        "status": node.status,
        "drifted": node.input_hash == NO_INPUT_HASH,
        "prevent_destroy": node.prevent_destroy,
        "depends_on": list(node.dependencies),
        "properties": node.properties,
        "outputs": outputs,
    }


def _print_mapping(title: str, values: Mapping[str, Any]) -> None:
    if not values:
        return
    console.print(f"  [bold]{title}:[/]")
    for key, value in sorted(values.items()):
        console.print(f"    {escape(key)} = {escape(str(value))}")


# -- unlock -------------------------------------------------------------------


def render_locks(held: Mapping[str, Lease]) -> None:
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


# -- fsck ---------------------------------------------------------------------


def render_fsck(report: FsckReport) -> bool:
    """Print what ``fsck`` found; ``True`` when something is left unrepaired.

    A lost head that ``--rebuild-heads`` re-pointed is listed once, as rebuilt.
    """
    console.print(f"checked {report.heads} head(s) and {report.entries} journal entr(ies)")
    for name, ref in report.missing:
        console.print(f"  [red]missing entry[/] {escape(name)}: {escape(ref)}")
    for name in report.unrepaired:
        console.print(f"  [red]lost head[/] {escape(name)} (entries past the snapshot)")
    for name, ref in report.rebuilt:
        console.print(
            f"  [yellow]rebuilt head[/] {escape(name)} -> {escape(ref)} "
            f"[dim](review: this entry may never have been committed)[/]"
        )
    if report.pending:
        console.print(
            f"  [dim]{len(report.pending)} pending entr(ies) (in-flight or crashed writes)[/]"
        )
    if report.collectable:
        console.print(f"  [dim]{report.collectable} entr(ies) awaiting compaction[/]")
    if report.healthy:
        console.print("[green]ok[/]")
    return not report.healthy
