"""Inline in-config cross-stack output references into ordinary Refs.

A ``StackReference("common").output("vpc_id")`` yields a :class:`StackOutputRef`
that is not a :class:`~atlantide.core.types.Ref`, so it never becomes a graph
edge — the referenced stack is applied separately and its output read from
committed state. When the referenced stack lives in the *same* config, its
output already holds a live value expression (``network.vpc_id``, a ``Ref``) in
``registry.outputs``. Substituting the handle with that expression
before lowering turns the cross-stack reference into an ordinary ref: the graph
gains a real edge, so dependent stacks order after their sources and independent
stacks still run in parallel, and the value threads through ``live_outputs`` at
apply — exactly like an intra-stack ref.

Only *in-config* references (target ``{stack}:{name}`` present in this config's
outputs) are inlined. An *external* reference (source stack in a separate config,
already applied) is left untouched, keeping the committed-outputs path.
"""

from __future__ import annotations

from typing import Any

from returns.result import Failure

from atlantide.core._tree import tree_any, tree_map
from atlantide.core.errors import StackOutputCycleError
from atlantide.core.resource import Resource, ResourceRegistry
from atlantide.core.types import StackOutputRef


def inline_stack_outputs(registry: ResourceRegistry) -> ResourceRegistry:
    """Return a registry with in-config ``StackOutputRef``s replaced by real refs.

    External refs and everything else pass through unchanged; when nothing in the
    config is an in-config cross-stack reference, the original registry is returned
    as-is. Raises :class:`StackOutputCycleError` on a cross-stack output cycle.
    """
    outputs = registry.outputs
    resources = registry.all()
    if not _has_any_inconfig_ref(resources, outputs):
        return registry

    resolved: dict[str, Any] = {}
    for key in outputs:
        _resolve_output_expr(key, outputs, resolved, ())

    def substitute(value: Any) -> Any:
        return tree_map(value, lambda v: _inline_leaf(v, outputs, resolved))

    rebuilt = ResourceRegistry()
    for res in resources:
        updates = {
            name: substitute(value)
            for name, value in res.input_values().items()
            if _has_inconfig_ref(value, outputs)
        }
        copy = res.model_copy(update=updates) if updates else res
        outcome = rebuilt.register(copy)
        if isinstance(outcome, Failure):
            raise outcome.failure()
    for key, value in resolved.items():
        rebuilt.add_output(key, value)
    for binding in registry.policy_bindings:
        rebuilt.add_policy_binding(binding)
    return rebuilt


def _resolve_output_expr(
    key: str, outputs: dict[str, Any], resolved: dict[str, Any], seen: tuple[str, ...]
) -> Any:
    """Fully resolve one output's value expression, inlining in-config refs.

    ``seen`` is the chain of output keys currently being resolved; a key that
    recurs signals a cross-stack cycle. Completed keys are memoized in ``resolved``.
    """
    if key in resolved:
        return resolved[key]
    if key in seen:
        raise StackOutputCycleError([*seen, key])
    chain = (*seen, key)
    result = tree_map(
        outputs[key],
        lambda v: _resolve_leaf(v, outputs, resolved, chain),
    )
    resolved[key] = result
    return result


def _resolve_leaf(
    value: Any, outputs: dict[str, Any], resolved: dict[str, Any], seen: tuple[str, ...]
) -> Any:
    """Replace an in-config ``StackOutputRef`` with its resolved target expression."""
    if isinstance(value, StackOutputRef):
        target = _key(value)
        if target in outputs:
            return _resolve_output_expr(target, outputs, resolved, seen)
    return value


def _inline_leaf(value: Any, outputs: dict[str, Any], resolved: dict[str, Any]) -> Any:
    if isinstance(value, StackOutputRef) and _key(value) in outputs:
        return resolved[_key(value)]
    return value


def _key(ref: StackOutputRef) -> str:
    return f"{ref.stack}:{ref.name}"


def _has_inconfig_ref(value: Any, outputs: dict[str, Any]) -> bool:
    return tree_any(
        value, lambda v: isinstance(v, StackOutputRef) and _key(v) in outputs
    )


def _has_any_inconfig_ref(resources: list[Resource], outputs: dict[str, Any]) -> bool:
    in_inputs = any(
        _has_inconfig_ref(value, outputs)
        for res in resources
        for value in res.input_values().values()
    )
    in_outputs = any(_has_inconfig_ref(value, outputs) for value in outputs.values())
    return in_inputs or in_outputs
