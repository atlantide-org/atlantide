"""atlantide.policy: modular, per-resource policy engine.

- ``enforce`` / ``@policy`` attach policies (config-level or class-level).
- ``PolicyProvider`` + ``PolicyRegistry`` evaluate them (native-Python builtin
  provider ships).
- Evaluated at plan time; ``mandatory`` violations block apply, ``advisory`` warn.
"""

from atlantide.policy.base import (
    PolicyContext,
    PolicyFn,
    PolicyProvider,
    PolicyResult,
    Violation,
)
from atlantide.policy.binding import class_bindings, enforce, policy
from atlantide.policy.builtin import (
    DENY_DESTROY,
    DENY_DESTROY_ALIAS,
    REQUIRE_SECRET_REFS,
    REQUIRE_TAGS,
    BuiltinPolicyProvider,
    default_policy_registry,
)
from atlantide.policy.registry import PolicyRegistry

__all__ = [
    "DENY_DESTROY",
    "DENY_DESTROY_ALIAS",
    "REQUIRE_SECRET_REFS",
    "REQUIRE_TAGS",
    "BuiltinPolicyProvider",
    "PolicyContext",
    "PolicyFn",
    "PolicyProvider",
    "PolicyRegistry",
    "PolicyResult",
    "Violation",
    "class_bindings",
    "default_policy_registry",
    "enforce",
    "policy",
]
