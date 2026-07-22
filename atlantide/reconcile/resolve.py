"""Handle resolution: Refs, secret handles, and stack-output handles -> values.

Everything here is pure with respect to providers — resolution reads live
outputs, the secrets registry, and committed stack outputs, never the cloud.
``reconstruct`` is the inverse of persistence: it rebuilds a live ``Resource``
from a stored :class:`StateNode` so delete/read/refresh can call providers with
a typed object.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from atlantide.core._tree import tree_any, tree_collect, tree_map
from atlantide.core.errors import ProviderError
from atlantide.core.fields import sensitive_fields
from atlantide.core.markers import (
    is_ref_marker,
    is_ref_or_marker,
    is_stack_output_marker,
    is_transform_marker,
    ref_from_marker,
    stack_output_from_marker,
    transform_from_marker,
)
from atlantide.core.node_id import field_scope, local_name_of, type_name_of
from atlantide.core.resource import Resource
from atlantide.core.types import Ref, SecretRef, StackOutputRef, Transform
from atlantide.reconcile.context import ApplyEnv, LiveOutputs
from atlantide.secrets import (
    SecretsRegistry,
    is_secret_ref_marker,
    secret_ref_from_marker,
)
from atlantide.state.backend import StateNode


def resolve_value(value: Any, outputs: LiveOutputs, *, strict: bool = True) -> Any:
    """Replace Ref objects and ``{"$ref": "id#attr"}`` markers with real values.

    ``strict`` (apply): a missing upstream output raises ``KeyError``; dependencies
    always resolve first, so absence indicates an internal error. ``strict=False``
    (rebuilding from partial state): a missing output leaves the value a ``Ref``,
    which delete and read do not consume.
    """

    def lookup(node_id: str, attr: str, fallback: Any) -> Any:
        if strict:
            return outputs[node_id][attr]
        bucket = outputs.get(node_id)
        return bucket[attr] if bucket is not None and attr in bucket else fallback

    def leaf(v: Any) -> Any:
        if isinstance(v, Transform):
            return _eval_transform(v.op, list(v.args), outputs, strict=strict)
        if is_transform_marker(v):
            return _eval_transform(*transform_from_marker(v), outputs, strict=strict)
        if isinstance(v, Ref):
            return lookup(v.node_id, v.attr, v)
        if is_ref_marker(v):
            ref = ref_from_marker(v)
            return lookup(ref.node_id, ref.attr, ref)
        return v

    return tree_map(value, leaf, include_sets=False)


def _eval_transform(op: str, args: list[Any], outputs: LiveOutputs, *, strict: bool) -> Any:
    """Evaluate a deferred ``$transform`` once its operand refs resolve.

    Operands resolve through ``resolve_value``, so nested refs and transforms are
    supported, then reduce through a fixed allowlist of pure ops. No arbitrary code
    runs, so apply stays deterministic.
    """
    resolved = [resolve_value(arg, outputs, strict=strict) for arg in args]
    reducer = _TRANSFORM_OPS.get(op)
    if reducer is None:
        raise ProviderError(f"unknown transform op {op!r}")
    return reducer(resolved)


#: Pure reducers over resolved operands. Every entry is deterministic and
#: side-effect free.
_TRANSFORM_OPS: dict[str, Callable[[list[Any]], Any]] = {
    "concat": lambda a: "".join(str(x) for x in a),
    "interpolate": lambda a: str(a[0]).format(*a[1:]),
    "join": lambda a: str(a[0]).join(str(x) for x in a[1]),
}


def needs_resolution(value: Any) -> bool:
    return tree_any(
        value, lambda v: is_ref_or_marker(v) or is_transform_marker(v), include_sets=False
    )


def resolve_refs(res: Resource, outputs: LiveOutputs) -> Resource:
    """A copy of ``res`` with every upstream-output Ref field resolved."""
    updates = {
        name: resolve_value(value, outputs)
        for name, value in res.input_values().items()
        if needs_resolution(value)
    }
    return res.model_copy(update=updates) if updates else res


def resolve_secret_refs(res: Resource, secrets: SecretsRegistry) -> Resource:
    """Replace each ``SecretRef``-valued field with its resolved plaintext (in-memory)."""
    updates = {
        name: secrets.resolve(value)
        for name, value in res.input_values().items()
        if isinstance(value, SecretRef)
    }
    return res.model_copy(update=updates) if updates else res


def resolve_stack_refs(
    res: Resource, stack_outputs: dict[str, Any], *, strict: bool = True
) -> Resource:
    """Replace each ``StackOutputRef`` field with the referenced stack's output.

    ``strict`` (apply) raises if the referenced output is absent; ``strict=False``
    (rebuilding from partial state) leaves the handle in place, which delete and
    read do not consume.
    """
    updates: dict[str, Any] = {}
    for name, value in res.input_values().items():
        if not isinstance(value, StackOutputRef):
            continue
        key = f"{value.stack}:{value.name}"
        if key in stack_outputs:
            updates[name] = stack_outputs[key]
        elif strict:
            raise ProviderError(
                f"stack output {key!r} not found — apply stack {value.stack!r} first"
            )
    return res.model_copy(update=updates) if updates else res


def reconstruct(node: StateNode, env: ApplyEnv, outputs: LiveOutputs) -> Resource:
    """Rebuild a Resource from persisted state, resolving its handles.

    ``$secret_ref`` and ``$stack_output`` markers become handle objects, which pass
    validation; ``$ref`` markers resolve against outputs. Handles then resolve to
    values (secrets to plaintext, stack refs to committed outputs) leniently, since
    delete and read do not require a missing cross-stack value.
    """
    cls = env.types.get(node.type)
    if cls is None:
        raise ProviderError(
            f"cannot delete {node.id!r}: resource type {node.type!r} is unavailable"
        )
    name = local_name_of(node.id)

    def rebuild(value: Any) -> Any:
        if is_secret_ref_marker(value):
            return secret_ref_from_marker(value)
        if is_stack_output_marker(value):
            return stack_output_from_marker(value)
        return resolve_value(value, outputs, strict=False)

    props: dict[str, Any] = {key: rebuild(value) for key, value in node.properties.items()}
    # Restore persisted outputs onto their computed fields so read/delete can use
    # them. Declared fields only, excluding any already set as inputs.
    for key, value in node.outputs.items():
        if key in cls.model_fields and key not in props:
            props[key] = env.secrets.unseal(value)  # sensitive outputs are sealed at rest
    res = resolve_secret_refs(cls(name, **props), env.secrets)
    return resolve_stack_refs(res, env.stack_outputs, strict=False)


def seal_outputs(
    outputs: dict[str, Any], cls: type[Resource], secrets: SecretsRegistry
) -> dict[str, Any]:
    """Seal the ``sensitive`` string fields of ``outputs`` for persistence.

    A no-op without install key material, leaving state byte-identical. Only
    sensitive computed outputs are sealed; ordinary outputs such as arns and ids
    persist in the clear.
    """
    sensitive = set(sensitive_fields(cls))
    if not sensitive:
        return outputs
    return {
        key: (secrets.seal(value) if key in sensitive and isinstance(value, str) else value)
        for key, value in outputs.items()
    }


def unseal_outputs(outputs: dict[str, Any], secrets: SecretsRegistry) -> dict[str, Any]:
    """Plaintext view of persisted outputs (unseals any ``{"$sealed": ...}`` value)."""
    return {key: secrets.unseal(value) for key, value in outputs.items()}


def sensitive_output_names(output_decls: dict[str, Any], env: ApplyEnv) -> frozenset[str]:
    """Declared-output names whose value derives from a ``sensitive`` field.

    A declared export is sensitive when any Ref it contains, in live or marker
    form, points at a field the resource type marks ``sensitive=True``. An unknown
    type is treated as sensitive so the value is redacted rather than exposed.
    """
    names: set[str] = set()
    for name, value in output_decls.items():
        for found in tree_collect(value, is_ref_or_marker, include_sets=False):
            ref = found if isinstance(found, Ref) else ref_from_marker(found)
            cls = env.types.get(type_name_of(ref.node_id))
            if cls is None or ref.attr in sensitive_fields(cls):
                names.add(name)
                break
    return frozenset(names)


def secret_digests(
    res: Resource, node_id: str, secrets: SecretsRegistry
) -> dict[str, str]:
    """Digest each resolved secret field's value for rotation detection.

    ``res`` is the apply-time resource with secret handles already resolved to
    plaintext, so the digest tracks the applied value; the value itself is never
    stored. Salted with the install salt held by ``secrets``.
    """
    raw = res.input_values()
    digests: dict[str, str] = {}
    for name in sensitive_fields(type(res)):
        value = raw.get(name)
        if isinstance(value, str):
            digests[name] = secrets.digest(field_scope(node_id, name), value)
    return digests
