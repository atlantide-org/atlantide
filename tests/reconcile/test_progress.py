"""``progress_sink``: the TUI's narrow callback fed from the run event stream."""

from __future__ import annotations

from atlantide.core.events import (
    LEASE_ACQUIRE,
    NODE_FAIL,
    NODE_FINISH,
    NODE_START,
    RUN_START,
    ApplyEvent,
)
from atlantide.reconcile import Action
from atlantide.reconcile.progress import Phase, progress_sink

A = "default:test.Box:a"


def _event(phase: str, node_id: str | None = A, action: str | None = "create") -> ApplyEvent:
    return ApplyEvent(run_id="r", at=0.0, phase=phase, node_id=node_id, action=action)


def test_node_events_become_progress_phases() -> None:
    seen: list[tuple[str, Action, str]] = []
    sink = progress_sink(lambda *update: seen.append(update))
    for phase in (NODE_START, NODE_FINISH, NODE_FAIL):
        sink(_event(phase))
    assert seen == [
        (A, Action.CREATE, Phase.START),
        (A, Action.CREATE, Phase.FINISH),
        (A, Action.CREATE, Phase.FAIL),
    ]


def test_run_and_lease_events_draw_nothing() -> None:
    seen: list[object] = []
    sink = progress_sink(lambda *update: seen.append(update))
    sink(_event(RUN_START, node_id=None, action=None))
    sink(_event(LEASE_ACQUIRE, node_id=None, action=None))
    sink(_event(NODE_START, action=None))
    assert seen == []
