"""The native-Python builtin policy provider + a default registry."""

from __future__ import annotations

from typing_extensions import override

from atlantide.core.actions import DESTRUCTIVE_ACTIONS
from atlantide.core.errors import PolicyConfigError
from atlantide.core.fields import Mutability, field_mutability, sensitive_fields
from atlantide.core.resource import Resource
from atlantide.policy.base import PolicyContext, PolicyFn, PolicyProvider, PolicyResult
from atlantide.policy.registry import PolicyRegistry

#: Policy names. The destroy guard is also reachable under its earlier name.
REQUIRE_TAGS = "require-tags"
REQUIRE_SECRET_REFS = "require-secret-refs"
DENY_DESTROY = "deny-destroy-in-protected"
DENY_DESTROY_ALIAS = "deny-destroy-in-prod"


def _names(ctx: PolicyContext, policy: str, param: str) -> frozenset[str]:
    """The binding's ``param`` as a set of names, validated.

    A bare string is one name, not a sequence of characters: ``frozenset("prod")``
    would otherwise mean four names, ``p``, ``r``, ``o``, and ``d``.
    """
    value = ctx.param(param, ())
    if isinstance(value, str):
        return frozenset({value})
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise PolicyConfigError(
            f"{policy}: `{param}` must be a name or a list of them, got {type(value).__name__}"
        )
    return frozenset(str(item) for item in value)


def _require_tags(ctx: PolicyContext) -> PolicyResult:
    """Require the tag keys the binding names, or any tag at all when it names none.

    ``enforce("require-tags", keys=["env", "owner"])`` demands those keys;
    ``enforce("require-tags")`` demands only that the resource is tagged. A key
    present with an empty value counts as missing — a blank tag carries nothing.
    """
    res = ctx.resource
    if res is None or "tags" not in type(res).model_fields:
        return PolicyResult.ok()  # not applicable
    tags = getattr(res, "tags", None)
    tags = tags if isinstance(tags, dict) else {}

    required = _names(ctx, REQUIRE_TAGS, "keys")
    if not required:
        return PolicyResult.ok() if tags else PolicyResult.fail(f"{ctx.node_id} has no tags")
    missing = sorted(key for key in required if not tags.get(key))
    if missing:
        return PolicyResult.fail(f"{ctx.node_id} is missing tag(s): {', '.join(missing)}")
    return PolicyResult.ok()


def _literal_secrets(res: Resource) -> list[str]:
    """Names of ``sensitive`` fields holding a plaintext value.

    Computed fields are skipped — their value comes from the provider, not the
    config — and so is an empty one, which is the field's unset default rather
    than a secret. Anything else non-``str`` is a handle (``SecretRef``, or a
    ``Ref`` resolved at apply) and carries no plaintext.
    """
    cls = type(res)
    mutability = field_mutability(cls)
    literal = []
    for name in sensitive_fields(cls):
        if mutability.get(name) is Mutability.COMPUTED:
            continue
        value = getattr(res, name, None)
        if isinstance(value, str) and value:
            literal.append(name)
    return sorted(literal)


def _require_secret_refs(ctx: PolicyContext) -> PolicyResult:
    """Every ``sensitive`` input must hold a handle, not a literal.

    Referencing a secret by name keeps plaintext out of the config, the IR,
    state, and the ``.atlas`` artifact; assigning the value directly puts it in
    all four.

    A field declared with :func:`~atlantide.core.fields.secret` is typed
    ``SecretRef | None``, so pydantic already refuses a literal there. This
    covers what the annotation does not: a field declared
    ``mutable(..., sensitive=True)`` on a plain ``str``, which a provider author
    is free to write and which accepts plaintext silently.
    """
    if ctx.resource is None:
        return PolicyResult.ok()  # pure DELETE: nothing is being declared
    literal = _literal_secrets(ctx.resource)
    if literal:
        return PolicyResult.fail(
            f"{ctx.node_id} assigns a literal to {', '.join(literal)}; "
            "use SecretRef(<name>) so the value stays out of config, IR, and state"
        )
    return PolicyResult.ok()


def _deny_destroy(ctx: PolicyContext) -> PolicyResult:
    """Deny destructive changes (DELETE/REPLACE) in the binding's ``stacks``.

    With no ``stacks`` the policy passes everything: which stacks are protected
    is the config author's call, so there is no name to assume.
    """
    if ctx.stack in _names(ctx, DENY_DESTROY, "stacks") and ctx.action in DESTRUCTIVE_ACTIONS:
        return PolicyResult.fail(
            f"{ctx.node_id}: {ctx.action.value} not allowed in protected stack {ctx.stack!r}"
        )
    return PolicyResult.ok()


class BuiltinPolicyProvider(PolicyProvider):
    """Ships a small set of native-Python policies.

    The configurable ones read their arguments from the binding, so the provider
    itself needs none::

        enforce("require-tags", keys=["env", "owner"])
        enforce("deny-destroy-in-protected", stacks=["prod"])
        enforce("require-secret-refs")
    """

    def __init__(self) -> None:
        self._policies: dict[str, PolicyFn] = {
            REQUIRE_TAGS: _require_tags,
            REQUIRE_SECRET_REFS: _require_secret_refs,
            DENY_DESTROY: _deny_destroy,
            DENY_DESTROY_ALIAS: _deny_destroy,
        }

    def register(self, name: str, fn: PolicyFn) -> None:
        self._policies[name] = fn

    @override
    def has(self, name: str) -> bool:
        return name in self._policies

    @override
    def evaluate(self, name: str, ctx: PolicyContext) -> PolicyResult:
        return self._policies[name](ctx)


def default_policy_registry() -> PolicyRegistry:
    """A registry with the builtin provider registered."""
    registry = PolicyRegistry()
    registry.register(BuiltinPolicyProvider())
    return registry
