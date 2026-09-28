"""Per-node progress reporting: the ``Phase`` enum and the callback shapes.

Progress callbacks are the narrow channel a display (the TUI) listens on; the run
event stream (:mod:`atlantide.core.events`) is the wide one. :func:`progress_sink`
adapts the first onto the second.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum

from atlantide.core.actions import Action
from atlantide.core.events import ApplyEvent, EventSink

__all__ = [
    "Phase",
    "ProgressCallback",
    "RefreshProgress",
    "no_progress",
    "no_refresh_progress",
    "progress_sink",
]


class Phase(StrEnum):
    """Progress phases reported for each node (see the callbacks below)."""

    START = "start"
    FINISH = "finish"
    FAIL = "fail"


#: Refresh progress callback: ``(node_id, phase)``.
type RefreshProgress = Callable[[str, str], None]

#: Apply progress callback: ``(node_id, action, phase)``. Invoked from
#: concurrent tasks in one asyncio thread.
type ProgressCallback = Callable[[str, Action, str], None]


def no_progress(node_id: str, action: Action, phase: str) -> None:
    """The :data:`ProgressCallback` used when the caller passes none."""


def no_refresh_progress(node_id: str, phase: str) -> None:
    """The :data:`RefreshProgress` used when the caller passes none."""


def progress_sink(callback: ProgressCallback) -> EventSink:
    """Adapt a ``(node_id, action, phase)`` progress callback onto the event stream.

    Lets the TUI keep its narrow signature while the executor emits every phase
    through a single path.
    """
    phases = {
        "node_start": Phase.START,
        "node_finish": Phase.FINISH,
        "node_fail": Phase.FAIL,
    }

    def emit(event: ApplyEvent) -> None:
        phase = phases.get(event.phase)
        if phase is None or event.node_id is None or event.action is None:
            return  # run- and lease-level events carry no node
        callback(event.node_id, Action(event.action), phase)

    return emit
