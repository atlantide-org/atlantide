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
from atlantide.policy.builtin import BuiltinPolicyProvider, default_policy_registry
from atlantide.policy.registry import PolicyRegistry

__all__ = [
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
