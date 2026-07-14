"""Engine value types: a compiled config and the plan produced from it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from atlantide.core import PolicyBinding, PolicyLevel, Resource
from atlantide.graph.model import DiGraph
from atlantide.ir.model import IRGraph
from atlantide.policy import Violation
from atlantide.reconcile import ChangeSet


@dataclass(frozen=True, slots=True)
class Compiled:
    ir: IRGraph
    graph: DiGraph
    hashes: dict[str, str]
    resources: dict[str, Resource]
    policy_bindings: tuple[PolicyBinding, ...] = ()
    outputs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Plan:
    changeset: ChangeSet
    compiled: Compiled
    violations: tuple[Violation, ...] = ()
    warnings: tuple[str, ...] = ()  # non-blocking planner notes (e.g. CBD fallback)

    @property
    def blocked(self) -> tuple[Violation, ...]:
        return tuple(v for v in self.violations if v.level is PolicyLevel.MANDATORY)
