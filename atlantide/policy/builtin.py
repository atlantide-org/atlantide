"""The native-Python builtin policy provider + a default registry."""

from __future__ import annotations

from atlantide.core.actions import DESTRUCTIVE_ACTIONS
from atlantide.policy.base import PolicyContext, PolicyFn, PolicyProvider, PolicyResult
from atlantide.policy.registry import PolicyRegistry

#: Stack name the deny-destroy-in-prod policy protects by default.
_PROD_STACK = "prod"


def _require_tags(ctx: PolicyContext) -> PolicyResult:
    """Every taggable resource must carry at least one tag."""
    res = ctx.resource
    if res is None or "tags" not in type(res).model_fields:
        return PolicyResult.ok()  # not applicable
    tags = getattr(res, "tags", None)
    if isinstance(tags, dict) and tags:
        return PolicyResult.ok()
    return PolicyResult.fail(f"{ctx.node_id} has no tags")


def _make_deny_destroy_in_prod(prod_stack: str = _PROD_STACK) -> PolicyFn:
    """Build a policy denying destructive changes (DELETE/REPLACE) in ``prod_stack``."""

    def _deny_destroy_in_prod(ctx: PolicyContext) -> PolicyResult:
        if ctx.stack == prod_stack and ctx.action in DESTRUCTIVE_ACTIONS:
            return PolicyResult.fail(
                f"{ctx.node_id}: {ctx.action.value} not allowed in {prod_stack}"
            )
        return PolicyResult.ok()

    return _deny_destroy_in_prod


class BuiltinPolicyProvider(PolicyProvider):
    """Ships a small set of native-Python policies."""

    def __init__(self, prod_stack: str = _PROD_STACK) -> None:
        self._policies: dict[str, PolicyFn] = {
            "require-tags": _require_tags,
            "deny-destroy-in-prod": _make_deny_destroy_in_prod(prod_stack),
        }

    def register(self, name: str, fn: PolicyFn) -> None:
        self._policies[name] = fn

    def has(self, name: str) -> bool:
        return name in self._policies

    def evaluate(self, name: str, ctx: PolicyContext) -> PolicyResult:
        return self._policies[name](ctx)


def default_policy_registry(prod_stack: str = _PROD_STACK) -> PolicyRegistry:
    """A registry with the builtin provider registered."""
    registry = PolicyRegistry()
    registry.register(BuiltinPolicyProvider(prod_stack))
    return registry
