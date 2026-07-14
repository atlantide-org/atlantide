"""Live per-node progress table for apply/deploy/destroy."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from rich.live import Live
from rich.table import Table

from atlantide.cli.console import console
from atlantide.cli.render import SIGN
from atlantide.core.node_id import short_id
from atlantide.reconcile import Action, ProgressCallback
from atlantide.reconcile.context import PHASE_FAIL, PHASE_FINISH, PHASE_START

_PROGRESS_STATE = {
    "waiting": "[dim]waiting[/]",
    PHASE_START: "[yellow]applying…[/]",
    PHASE_FINISH: "[green]done[/]",
    PHASE_FAIL: "[red]failed[/]",
}


@contextmanager
def live_apply(actionable: list[tuple[str, Action]]) -> Iterator[ProgressCallback]:
    """A Rich live table advancing each node waiting → applying… → done/failed.

    Pre-seed with the known changes (apply) to show a full waiting list, or pass
    ``[]`` (deploy) to have rows appear as their nodes start.
    """
    order = [node_id for node_id, _ in actionable]
    action_of = dict(actionable)
    status: dict[str, str] = {node_id: "waiting" for node_id in order}

    def render() -> Table:
        table = Table.grid(padding=(0, 2))
        for node_id in order:
            sign, color = SIGN[action_of[node_id]]
            table.add_row(
                f"[{color}]{sign}[/]", short_id(node_id), _PROGRESS_STATE[status[node_id]]
            )
        return table

    with Live(render(), console=console, refresh_per_second=12, transient=False) as live:

        def callback(node_id: str, action: Action, phase: str) -> None:
            if node_id not in action_of:  # lazy row (deploy: no pre-seeded list)
                action_of[node_id] = action
                order.append(node_id)
            status[node_id] = phase
            live.update(render())

        yield callback
