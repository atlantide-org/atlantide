"""Engine value types: a compiled config and the plan produced from it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from atlantide.core import PolicyBinding, PolicyLevel, Resource
from atlantide.graph.model import DiGraph
from atlantide.ir.model import IRGraph
from atlantide.policy import Violation
from atlantide.reconcile import ChangeSet, Desired


@dataclass(frozen=True, slots=True)
class Compiled:
    ir: IRGraph
    graph: DiGraph
    hashes: dict[str, str]
    resources: dict[str, Resource]
    policy_bindings: tuple[PolicyBinding, ...] = ()
    outputs: dict[str, Any] = field(default_factory=dict)
    #: The config inputs this evaluation read, shown above the plan to explain
    #: differences between plans.
    inputs: dict[str, Any] = field(default_factory=dict)
    #: Every environment the config's ``Config`` declared, and the subset this
    #: run selected. Equal, or both empty, when nothing was narrowed.
    envs_declared: tuple[str, ...] = ()
    envs_selected: tuple[str, ...] = ()

    @property
    def envs_excluded(self) -> tuple[str, ...]:
        """Environments the config declared that this run did not select.

        Empty unless ``--env`` narrowed the run. The planner keeps their existing
        state out of the diff, where it would otherwise plan as a delete; the plan
        header names them.
        """
        selected = set(self.envs_selected)
        return tuple(name for name in self.envs_declared if name not in selected)

    def desired(self) -> Desired:
        """This config as the executor consumes it."""
        return Desired(
            ir=self.ir,
            graph=self.graph,
            hashes=self.hashes,
            resources=self.resources,
            output_decls=self.outputs,
        )


@dataclass(frozen=True, slots=True)
class Plan:
    changeset: ChangeSet
    compiled: Compiled
    violations: tuple[Violation, ...] = ()
    warnings: tuple[str, ...] = ()  # non-blocking planner notes (e.g. CBD fallback)

    @property
    def blocked(self) -> tuple[Violation, ...]:
        return tuple(v for v in self.violations if v.level is PolicyLevel.MANDATORY)
