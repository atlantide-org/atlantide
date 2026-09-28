"""Reconciliation: diff (Merkle skip), prevent_destroy guard, apply, refresh, adopt, aliases."""

from atlantide.core.actions import DESTRUCTIVE_ACTIONS, Action
from atlantide.reconcile.adopt import (
    AdoptOptions,
    ImportOutcome,
    ImportRequest,
    adopt,
    identity_fields,
)
from atlantide.reconcile.aliases import alias_remap, persist_migration, resolve_aliases
from atlantide.reconcile.changes import Change, ChangeSet, restrict, type_mutability
from atlantide.reconcile.classify import reclassify
from atlantide.reconcile.diff import diff
from atlantide.reconcile.env import ApplyEnv, Desired, OnFailure
from atlantide.reconcile.executor import apply
from atlantide.reconcile.guards import check_prevent_destroy, deferred_to_apply
from atlantide.reconcile.progress import ProgressCallback, RefreshProgress
from atlantide.reconcile.refresh import Drift, DriftReport, NodeDrift, refresh
from atlantide.reconcile.report import ApplyReport

__all__ = [
    "DESTRUCTIVE_ACTIONS",
    "Action",
    "AdoptOptions",
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
    "check_prevent_destroy",
    "deferred_to_apply",
    "diff",
    "identity_fields",
    "persist_migration",
    "reclassify",
    "refresh",
    "resolve_aliases",
    "restrict",
    "type_mutability",
]
