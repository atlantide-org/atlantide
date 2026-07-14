"""Enforce the ``prevent_destroy`` guard on a ChangeSet.

Ordering is delegated to the graph scheduler at execution time. Any DELETE or
REPLACE removing a protected resource fails the whole plan before any mutation.
"""

from __future__ import annotations

from collections.abc import Set

from returns.result import Failure, Result, Success

from atlantide.core.errors import PreventDestroyError
from atlantide.reconcile.diff import DESTRUCTIVE_ACTIONS, ChangeSet


def plan(changeset: ChangeSet, protected: Set[str]) -> Result[ChangeSet, PreventDestroyError]:
    """Validate the ChangeSet; Failure if a destructive action hits a protected id."""
    blocked = [
        c.node_id
        for c in changeset.changes
        if c.action in DESTRUCTIVE_ACTIONS and c.node_id in protected
    ]
    if blocked:
        joined = ", ".join(sorted(blocked))
        return Failure(
            PreventDestroyError(f"prevent_destroy blocks destroying: {joined}")
        )
    return Success(changeset)
