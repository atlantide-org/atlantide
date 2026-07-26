"""What a run did, as a stream of events.

The executor already reported per-node progress, but only in the shape a terminal
needed: ``(node_id, action, phase)``, with no timing, no error, and no way to tell
one run from another. That is enough to draw a table and not enough to answer
"who changed this, when, and what happened" — which is the question asked after
something has gone wrong, by someone who was not there.

:class:`ApplyEvent` widens it just far enough to answer that, and the progress
callback becomes an adapter over the same stream rather than a second, parallel
notification path that can drift from it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: A run starts and ends. The header carries the identity of the run itself.
RUN_START = "run_start"
RUN_FINISH = "run_finish"

#: Per-node, mirroring the progress phases the TUI already consumed.
NODE_START = "node_start"
NODE_FINISH = "node_finish"
NODE_FAIL = "node_fail"

#: Lock lifecycle: who held state when, which anchors an incident timeline.
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
    reconstructed stream carries the times it actually had.
    """

    run_id: str
    at: float
    phase: str
    node_id: str | None = None
    action: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


#: Where events go. A single function, so an S3 or webhook sink is a drop-in
#: rather than a new interface.
EventSink = Callable[[ApplyEvent], None]


def no_sink(event: ApplyEvent) -> None:
    """Discard. The default, so nothing pays for the stream unless it is wanted."""


def fanout(*sinks: EventSink) -> EventSink:
    """One sink feeding several — the terminal display and the audit file at once.

    A sink that raises must not take the run down with it: an audit file on a
    full disk is a problem, but it is a smaller problem than an apply aborting
    half-way because of one.
    """

    def emit(event: ApplyEvent) -> None:
        for sink in sinks:
            try:
                sink(event)
            except Exception:
                continue

    return emit
