"""``atlantide state unlock`` — show and break leases a dead run left behind."""

from __future__ import annotations

from contextlib import closing
from typing import Annotated

import typer

from atlantide.cli.commands.state.common import announced_target
from atlantide.cli.console import console
from atlantide.cli.errors import fail
from atlantide.cli.options import ConfirmOpt, require_confirm
from atlantide.cli.views.state import render_locks
from atlantide.state import Lease

__all__ = ["unlock"]

NodeOpt = Annotated[
    list[str] | None,
    typer.Option("--node", help="Break the hold on this node id (repeatable)."),
]
OwnerOpt = Annotated[
    str | None, typer.Option("--owner", help="Break every hold held by this owner.")
]
AllOpt = Annotated[
    bool,
    typer.Option("--all", help="Break every hold recorded for this state."),
]


def unlock(
    node: NodeOpt = None,
    owner: OwnerOpt = None,
    every: AllOpt = False,
    confirm: ConfirmOpt = False,
) -> None:
    """Show who holds the state lock, and break a hold left behind by a dead run.

    A lease outlives the run that took it: a killed CI job blocks other runs until
    the TTL lapses. With no selector this only lists the holds. Breaking a hold
    while its run is alive lets two applies write the same resources, so this
    names the holder and asks first.

    Only this state's holds are listed and broken; projects sharing one lock
    table do not see or break each other's.
    """
    target = announced_target()
    with closing(target.open()) as backend:
        held = backend.locks()
        if not held:
            console.print("[dim]no locks held[/]")
            return
        render_locks(held)
        if not (node or owner or every):
            console.print(
                "\n[dim]pass --node/--owner/--all to break a hold "
                "(only when you know the run is gone)[/]"
            )
            return
        targets = _selected(held, node, owner, every=every)
        require_confirm(f"\nBreak {len(targets)} lock(s)?", confirm=confirm)
        broken = backend.force_unlock(targets)
    console.print(f"[green]unlocked[/] {broken} node(s)")


def _selected(
    held: dict[str, Lease],
    nodes: list[str] | None,
    owner: str | None,
    *,
    every: bool,
) -> set[str]:
    """Return the node ids the selectors name.

    A selector that matches nothing is an error, so a typo does not pass as success.
    """
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
