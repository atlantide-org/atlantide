"""Live per-node progress table for apply/deploy/destroy."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from rich.table import Table

from atlantide.cli.console import console
from atlantide.cli.views.common import SIGN
from atlantide.core.actions import Action
from atlantide.core.node_id import short_id
from atlantide.reconcile import ProgressCallback
from atlantide.reconcile.progress import Phase

#: Status of a planned node whose work has not started. Not an executor phase:
#: every pre-seeded row starts in it.
WAITING = "waiting"

_PROGRESS_STATE = {
    WAITING: "[dim]waiting[/]",
    Phase.START: "[yellow]applying…[/]",
    Phase.FINISH: "[green]done[/]",
    Phase.FAIL: "[red]failed[/]",
}

#: Above this many nodes the table shows only active rows plus a counts line, since
#: a longer table exceeds the terminal height and is cropped on every frame. The
#: final report lists every node.
FULL_LIST_MAX = 40

#: Rows drawn at once in windowed mode, so a run that fails in bulk cannot grow
#: the table back to O(nodes).
_WINDOW_MAX = FULL_LIST_MAX


class ProgressTable:
    """Per-node apply progress, rendered on Rich's refresh tick.

    ``Live`` re-renders at ``refresh_per_second``, so building the table in
    ``__rich__`` rather than in the progress callback makes drawing cost
    independent of node count: about 12 tables a second instead of two per node.
    Recording a phase is O(1) and draws nothing.

    ``record`` runs on the event loop's thread and ``__rich__`` on Rich's refresh
    thread, so both hold :attr:`_lock`.
    """

    def __init__(self, actionable: list[tuple[str, Action]]) -> None:
        self._order = [node_id for node_id, _ in actionable]
        self._action_of = dict(actionable)
        self._status: dict[str, str] = {node_id: WAITING for node_id in self._order}
        self._in_flight: dict[str, None] = {}  # insertion-ordered set
        self._failed: dict[str, None] = {}
        #: Finished node ids in completion order; the windowed table draws the last
        #: :data:`_WINDOW_MAX`. The full list is kept so :meth:`record` stays O(1).
        self._done: list[str] = []
        self._lock = threading.Lock()

    def record(self, node_id: str, action: Action, phase: str) -> None:
        """The :type:`ProgressCallback`: note a node's new phase, draw nothing."""
        with self._lock:
            if node_id not in self._action_of:  # lazy row (deploy: no pre-seeded list)
                self._order.append(node_id)
            # A finish carries the action taken, which a conditional replace the
            # apply found unnecessary lowers to an update or a noop.
            if node_id not in self._action_of or phase == Phase.FINISH:
                self._action_of[node_id] = action
            self._status[node_id] = phase
            self._in_flight.pop(node_id, None)
            if phase == Phase.START:
                self._in_flight[node_id] = None
            elif phase == Phase.FINISH:
                self._done.append(node_id)
            elif phase == Phase.FAIL:
                self._failed[node_id] = None

    def _window(self) -> tuple[list[str], int]:
        """The node ids to draw, and how many were left out.

        Ordered failures, then work in flight, then the latest finishes; the cap
        truncates from the end, so failures are dropped last.
        """
        if len(self._order) <= FULL_LIST_MAX:
            return self._order, 0
        shown = [*self._failed, *self._in_flight]
        room = _WINDOW_MAX - len(shown)
        if room > 0:
            shown += self._done[-room:]
        elif room < 0:
            shown = shown[:_WINDOW_MAX]
        return shown, len(self._order) - len(shown)

    def _counts(self) -> str:
        """The tallies standing in for the rows a windowed table does not draw."""
        tally = f"[green]{len(self._done)}[/]/{len(self._order)} done"
        if self._failed:
            tally += f" · [red]{len(self._failed)} failed[/]"
        return f"[dim]{tally}[/]"

    def __rich__(self) -> Table:
        table = Table.grid(padding=(0, 2))
        with self._lock:
            shown, elided = self._window()
            for node_id in shown:
                sign, color = SIGN[self._action_of[node_id]]
                table.add_row(
                    f"[{color}]{sign}[/]",
                    short_id(node_id),
                    _PROGRESS_STATE[self._status[node_id]],
                )
            if len(self._order) > FULL_LIST_MAX:
                table.add_row("", f"[dim]…{elided} more[/]", self._counts())
        return table


@contextmanager
def live_apply(actionable: list[tuple[str, Action]]) -> Iterator[ProgressCallback]:
    """A Rich live table advancing each node waiting → applying… → done/failed.

    Pre-seed with the known changes (apply) to show a full waiting list, or pass
    ``[]`` (deploy) to have rows appear as their nodes start.
    """
    # Imported lazily so other commands (`--help` included) skip the startup cost.
    from rich.live import Live

    progress = ProgressTable(actionable)
    with Live(progress, console=console, refresh_per_second=12, transient=False):
        yield progress.record


@contextmanager
def maybe_live(
    actionable: list[tuple[str, Action]], *, enabled: bool
) -> Iterator[ProgressCallback | None]:
    """:func:`live_apply` when there is a terminal to draw on, otherwise nothing.

    Lets callers wrap the engine call in one ``with`` instead of repeating it per
    branch.
    """
    if not enabled:
        yield None
        return
    with live_apply(actionable) as progress:
        yield progress
