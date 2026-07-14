"""Policy engine interfaces: a policy checks one node's pending change.

A :class:`PolicyProvider` evaluates named policies. Concrete providers plug into
a ``PolicyRegistry``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

from atlantide.core import PolicyLevel, Resource
from atlantide.core.actions import Action


@dataclass(frozen=True, slots=True)
class PolicyContext:
    """What a policy sees: the node, its pending action, and its desired state."""

    node_id: str
    action: Action
    stack: str
    resource: Resource | None  # desired resource; None for a pure DELETE


@dataclass(frozen=True, slots=True)
class PolicyResult:
    passed: bool
    message: str = ""

    @classmethod
    def ok(cls) -> PolicyResult:
        return cls(passed=True)

    @classmethod
    def fail(cls, message: str) -> PolicyResult:
        return cls(passed=False, message=message)


@dataclass(frozen=True, slots=True)
class Violation:
    policy: str
    level: PolicyLevel
    node_id: str
    message: str


#: A policy check: pure function of the context.
PolicyFn = Callable[[PolicyContext], PolicyResult]


class PolicyProvider(ABC):
    """Evaluates named policies. Deterministic (runs at plan time)."""

    @abstractmethod
    def has(self, name: str) -> bool:
        """Whether this provider defines a policy called ``name``."""

    @abstractmethod
    def evaluate(self, name: str, ctx: PolicyContext) -> PolicyResult:
        """Run policy ``name`` against ``ctx``."""
