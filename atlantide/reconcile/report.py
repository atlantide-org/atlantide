"""What one run did: the executor's per-action report."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ApplyReport:
    """Node ids per action taken, plus failure details and declared exports.

    ``outputs`` holds the resolved declared exports (``output()`` calls). Live
    per-node values are ``LiveOutputs``; committed cross-stack values are
    ``StateBackend.outputs()``.
    """

    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    noop: list[str] = field(default_factory=list)
    #: Nodes whose state row this run rewrote without calling their provider for a
    #: ``prevent_destroy`` change (a state-only NOOP). Also listed under ``noop``.
    state_only: list[str] = field(default_factory=list)
    #: node id -> the action actually taken (``"update"`` or ``"noop"``) for a
    #: conditional REPLACE the apply found unnecessary once its refs resolved. The
    #: node is listed under that action, not under ``replaced``.
    downgraded: dict[str, str] = field(default_factory=dict)
    rolled_back: list[str] = field(default_factory=list)  # compensated on saga rollback
    #: node id -> why its compensation failed, leaving state and the provider out of
    #: sync. Also raised as :class:`~atlantide.core.errors.RollbackError`.
    rollback_failed: dict[str, str] = field(default_factory=dict)
    #: node id -> why the row could not be marked stale after a failed compensation
    #: or delete. The next plan reports NOOP for an incorrect row; see
    #: :meth:`~atlantide.reconcile.executor.records.NodeRecords.mark_stale`.
    poison_failed: dict[str, str] = field(default_factory=dict)
    #: node id -> what is still live at the provider with no state row. Unlike a
    #: poisoned row, the next plan cannot surface this.
    orphaned: dict[str, str] = field(default_factory=dict)
    #: Why the saga did not run despite ``on_failure="rollback"``, or ``None`` if it
    #: ran. See :meth:`~atlantide.reconcile.executor.saga.Saga.blocker`.
    rollback_skipped: str | None = None
    outputs: dict[str, Any] = field(default_factory=dict)
    #: Output names whose value derives from a sensitive field; renderers redact these.
    sensitive_outputs: frozenset[str] = frozenset()
