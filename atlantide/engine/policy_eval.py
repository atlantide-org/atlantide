"""Policy evaluation over a finished plan's actionable changes.

A node's policies are the config's bindings that apply to its type plus the
ones its resource class declares (:func:`~atlantide.policy.class_bindings`).
The registry raises on a provider error; the planner turns that into the
plan's ``Result``.
"""

from __future__ import annotations

from collections.abc import Mapping

from atlantide.core import PolicyBinding, Resource
from atlantide.core.node_id import stack_of
from atlantide.engine.model import Compiled
from atlantide.policy import PolicyContext, PolicyRegistry, Violation, class_bindings
from atlantide.reconcile import ChangeSet


def evaluate_policies(
    changeset: ChangeSet,
    compiled: Compiled,
    *,
    types: Mapping[str, type[Resource]],
    policies: PolicyRegistry,
) -> tuple[Violation, ...]:
    """Every violation of the policies bound to each actionable change's type."""
    violations: list[Violation] = []
    for change in changeset.actionable:
        node = change.desired or change.prior
        type_name = node.type if node is not None else ""
        for binding in _bindings_for(type_name, compiled, types):
            # Rebuilt per binding: a parameterised policy bound twice (one
            # stack set per environment) must see each binding's arguments.
            ctx = PolicyContext(
                node_id=change.node_id,
                action=change.action,
                stack=stack_of(change.node_id),
                resource=compiled.resources.get(change.node_id),
                params=binding.params,
            )
            result = policies.evaluate(binding.name, ctx)
            if not result.passed:
                violations.append(
                    Violation(binding.name, binding.level, change.node_id, result.message)
                )
    return tuple(violations)


def _bindings_for(
    type_name: str, compiled: Compiled, types: Mapping[str, type[Resource]]
) -> list[PolicyBinding]:
    config_bindings = [b for b in compiled.policy_bindings if b.applies_to(type_name)]
    cls = types.get(type_name)
    decorated = list(class_bindings(cls)) if cls is not None else []
    return config_bindings + decorated
