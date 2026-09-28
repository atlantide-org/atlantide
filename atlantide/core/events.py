"""What a run did, as a stream of events.

Each :class:`ApplyEvent` carries the run id, a timestamp, the node and action
and any error detail: enough to answer "who changed this, when, and what
happened" after the fact. The terminal progress display is an adapter over this
same stream rather than a second notification path, so the two cannot drift apart.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: Run boundaries: the first and last event of every run.
RUN_START = "run_start"
RUN_FINISH = "run_finish"

#: Per-node, mirroring the progress phases the TUI consumes.
NODE_START = "node_start"
NODE_FINISH = "node_finish"
NODE_FAIL = "node_fail"

#: Lock lifecycle: who held state, and when.
LEASE_ACQUIRE = "lease_acquire"
LEASE_RENEW = "lease_renew"
LEASE_LOST = "lease_lost"

#: Compensation: what a failed run undid, and what it could not.
ROLLBACK_START = "rollback_start"
ROLLBACK_NODE = "rollback_node"
ROLLBACK_SKIPPED = "rollback_skipped"


@dataclass(frozen=True, slots=True)
class ApplyEvent:
    """One thing that happened during a run.

    ``at`` is supplied by the emitter rather than read here, so a replayed or
    reconstructed stream carries its original times.
    """

    run_id: str
    at: float
    phase: str
    node_id: str | None = None
    action: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


#: Where events go. A plain callable, so a new sink (S3, webhook) needs no new
#: interface.
type EventSink = Callable[[ApplyEvent], None]


def no_sink(event: ApplyEvent) -> None:
    """Discard the event. The default, so the stream costs nothing unless used."""


def fanout(*sinks: EventSink) -> EventSink:
    """One sink feeding several, e.g. the terminal display and the audit file.

    A sink that raises is skipped: a failing sink (e.g. an audit file on a full
    disk) must not abort an apply half-way.
    """

    def emit(event: ApplyEvent) -> None:
        for sink in sinks:
            try:
                sink(event)
            except Exception:
                continue

    return emit
