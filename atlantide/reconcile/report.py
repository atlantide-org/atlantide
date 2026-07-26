"""What one run did: the executor's per-action report."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ApplyReport:
    """What one run did, per action.

    ``outputs`` holds the resolved declared exports (``output()`` calls). Live
    per-node values are ``LiveOutputs``; committed cross-stack values are
    ``StateBackend.outputs()``.
    """

    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    noop: list[str] = field(default_factory=list)
    rolled_back: list[str] = field(default_factory=list)  # compensated on saga rollback
    #: node id -> why its compensation failed, leaving state and the provider
    #: disagreeing. Also raised as :class:`RollbackError`.
    rollback_failed: dict[str, str] = field(default_factory=dict)
    #: node id -> why the row could not be marked stale after a failed compensation
    #: or delete. The next plan reports NOOP despite state being wrong; see
    #: :meth:`_Applier._poison`.
    poison_failed: dict[str, str] = field(default_factory=dict)
    #: node id -> what is still live at the provider with no state row describing
    #: it. Unlike a poisoned row, the next plan cannot surface this.
    orphaned: dict[str, str] = field(default_factory=dict)
    #: Why the saga did not run despite ``on_failure="rollback"``, or ``None`` if it
    #: did. See :meth:`_Applier._rollback_blocker`.
    rollback_skipped: str | None = None
    outputs: dict[str, Any] = field(default_factory=dict)  # declared exports, resolved
    #: Output names whose value derives from a sensitive field; renderers redact these.
    sensitive_outputs: frozenset[str] = frozenset()
