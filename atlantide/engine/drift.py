"""Plan-drift detection: refuse to execute a changeset that was not approved."""

from __future__ import annotations

from atlantide.core.errors import PlanDriftError
from atlantide.reconcile import ChangeSet


def raise_drift(approved: ChangeSet, fresh: ChangeSet) -> None:
    """Refuse to execute a changeset that is not the one that was approved.

    Called with the lease held, after the re-diff. State moved between the plan a
    human read and the plan about to run — another apply landed, or a resource
    was destroyed out of band — so the diff legitimately changed. What is not
    legitimate is doing it anyway without saying so.
    """
    before, after = approved.fingerprint(), fresh.fingerprint()
    if before == after:
        return
    added = sorted(f"{action} {node_id}" for node_id, action, *_ in after - before)
    removed = sorted(f"{action} {node_id}" for node_id, action, *_ in before - after)
    parts = []
    if added:
        parts.append(f"now also: {', '.join(added)}")
    if removed:
        parts.append(f"no longer: {', '.join(removed)}")
    raise PlanDriftError(
        "state changed between the plan you approved and the lock being taken, so "
        "the changes are no longer the ones shown — " + "; ".join(parts) + ". "
        "Re-run to plan against current state."
    )
