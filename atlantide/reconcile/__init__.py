"""atlantide.reconcile: diff (Merkle-skip), planner (guards), executor (apply)."""

from atlantide.reconcile.adopt import (
    ImportOutcome,
    ImportRequest,
    adopt,
    identity_fields,
)
from atlantide.reconcile.aliases import alias_remap, persist_migration, resolve_aliases
from atlantide.reconcile.context import (
    ApplyEnv,
    Desired,
    OnFailure,
    ProgressCallback,
    RefreshProgress,
)
from atlantide.reconcile.diff import (
    DESTRUCTIVE_ACTIONS,
    Action,
    Change,
    ChangeSet,
    diff,
    force_replace,
    restrict,
)
from atlantide.reconcile.executor import apply
from atlantide.reconcile.planner import plan
from atlantide.reconcile.refresh import (
    Drift,
    DriftReport,
    NodeDrift,
    classify_drift,
    refresh,
    resolved_properties,
)
from atlantide.reconcile.report import ApplyReport

__all__ = [
    "DESTRUCTIVE_ACTIONS",
    "Action",
    "ApplyEnv",
    "ApplyReport",
    "Change",
    "ChangeSet",
    "Desired",
    "Drift",
    "DriftReport",
    "ImportOutcome",
    "ImportRequest",
    "NodeDrift",
    "OnFailure",
    "ProgressCallback",
    "RefreshProgress",
    "adopt",
    "alias_remap",
    "apply",
    "classify_drift",
    "diff",
    "force_replace",
    "identity_fields",
    "persist_migration",
    "plan",
    "refresh",
    "resolve_aliases",
    "resolved_properties",
    "restrict",
]
