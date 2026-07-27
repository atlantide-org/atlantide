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

#: A node that has been planned but whose work has not started. Not a phase the
#: executor reports — it is the state every pre-seeded row begins in.
WAITING = "waiting"

_PROGRESS_STATE = {
    WAITING: "[dim]waiting[/]",
    PHASE_START: "[yellow]applying…[/]",
    PHASE_FINISH: "[green]done[/]",
    PHASE_FAIL: "[red]failed[/]",
}

#: Above this many nodes the table stops listing every row and shows only the
#: ones doing something, plus a counts line. A run this wide is already taller
#: than any terminal, so the remaining rows were built and then cropped away
#: unseen — on every frame. The full report at the end lists everything.
FULL_LIST_MAX = 40

#: Rows drawn at once in windowed mode, so a run that fails in bulk cannot grow
#: the table back to O(nodes).
_WINDOW_MAX = FULL_LIST_MAX


class ProgressTable:
    """Per-node apply progress, rendered on Rich's refresh tick.

    ``Live`` re-renders its renderable at ``refresh_per_second``, so building the
    table inside ``__rich__`` rather than inside the progress callback decouples
    the cost of drawing from the number of nodes: a 2000-node apply builds ~12
    tables a second instead of one per phase change (two per node). Recording a
    phase is O(1) and draws nothing.
    """

    def __init__(self, actionable: list[tuple[str, Action]]) -> None:
        self._order = [node_id for node_id, _ in actionable]
        self._action_of = dict(actionable)
        self._status: dict[str, str] = {node_id: WAITING for node_id in self._order}
        self._in_flight: dict[str, None] = {}  # insertion-ordered set
        self._failed: dict[str, None] = {}
        #: Finished node ids in completion order, so the windowed table can show a
        #: moving tail rather than a static counter. Only the last
        #: :data:`_WINDOW_MAX` are ever drawn, but keeping the whole list costs one
        #: append per node and keeps `record` O(1).
        self._done: list[str] = []

    def record(self, node_id: str, action: Action, phase: str) -> None:
        """The :type:`ProgressCallback`: note a node's new phase, draw nothing."""
        if node_id not in self._action_of:  # lazy row (deploy: no pre-seeded list)
            self._action_of[node_id] = action
            self._order.append(node_id)
        self._status[node_id] = phase
        self._in_flight.pop(node_id, None)
        if phase == PHASE_START:
            self._in_flight[node_id] = None
        elif phase == PHASE_FINISH:
            self._done.append(node_id)
        elif phase == PHASE_FAIL:
            self._failed[node_id] = None

    def _window(self) -> tuple[list[str], int]:
        """The node ids to draw, and how many were left out.

        Ordered by how much they want attention — failures, then work in flight,
        then a tail of what just finished — because the cap drops from the end.
        A failure is therefore the last thing to go, never the first.
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
        shown, elided = self._window()
        for node_id in shown:
            sign, color = SIGN[self._action_of[node_id]]
            table.add_row(
                f"[{color}]{sign}[/]", short_id(node_id), _PROGRESS_STATE[self._status[node_id]]
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
    progress = ProgressTable(actionable)
    # No `live.update` per event: `Live` holds this object and re-renders it on
    # its own refresh tick, which is what keeps drawing off the hot path.
    with Live(progress, console=console, refresh_per_second=12, transient=False):
        yield progress.record


@contextmanager
def maybe_live(
    actionable: list[tuple[str, Action]], *, enabled: bool
) -> Iterator[ProgressCallback | None]:
    """:func:`live_apply` when there is a terminal to draw on, otherwise nothing.

    Without this, every caller writes the branch — and because the engine call
    sits *inside* the ``with``, both arms have to repeat it in full. Three
    commands did, at seven keyword arguments each, which is three places for an
    argument to be added to one arm and forgotten in the other.
    """
    if not enabled:
        yield None
        return
    with live_apply(actionable) as progress:
        yield progress
