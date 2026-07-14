"""The change-action vocabulary shared by the diff, policies, and rendering.

Lives in ``core`` (not ``reconcile``) so plan-time consumers like the policy
engine depend down on core rather than sideways on the reconciliation engine.
"""

from __future__ import annotations

import enum


class Action(enum.StrEnum):
    CREATE = "create"
    UPDATE = "update"
    REPLACE = "replace"
    DELETE = "delete"
    NOOP = "noop"


# Destructive actions (fully or partially remove a resource) — gated by
# prevent_destroy and the deny-destroy-in-prod policy.
DESTRUCTIVE_ACTIONS = frozenset({Action.DELETE, Action.REPLACE})
