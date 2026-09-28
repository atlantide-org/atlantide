"""The Ref/marker codec: one home for handle <-> ``{"$...": ...}`` conversions.

Live handle objects (:class:`~atlantide.core.types.Ref`,
:class:`~atlantide.core.types.SecretRef`,
:class:`~atlantide.core.types.StackOutputRef`) serialize to single-key dict
*markers* via their ``canonical()`` methods; IR, state, and artifacts carry the
markers. This module owns the constants, the strict per-marker predicates and
parsers, and the tree-level conversions.

Three distinct ref-detection predicates answer different questions:

- :func:`contains_ref`: a live ``Ref`` object anywhere (pre-lowering values);
- :func:`has_ref_key`: a dict with a ``"$ref"`` key anywhere (canonicalized
  IR/state trees, loose match used by the diff);
- :func:`is_ref_or_marker`: a single value that stands in for an upstream
  output, in either form (the executor's resolution test).

``$secret_ref`` markers are owned by :mod:`atlantide.secrets` (import
``is_secret_ref_marker``/``secret_ref_from_marker`` from there).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from atlantide.core import _tree
from atlantide.core._tree import tree_any, tree_collect, tree_map
from atlantide.core.types import (
    HANDLES,
    REF_KEY,
    STACK_OUTPUT_KEY,
    TRANSFORM_KEY,
    Ref,
    StackOutputRef,
    Transform,
)

__all__ = [
    "REF_KEY",
    "STACK_OUTPUT_KEY",
    "TRANSFORM_KEY",
    "canonicalize",
    "collect_ref_targets",
    "collect_refs",
    "contains_handle",
    "contains_ref",
    "has_ref_key",
    "is_ref_marker",
    "is_ref_or_marker",
    "is_stack_output_marker",
    "is_transform_marker",
    "ref_from_marker",
    "refs_to_markers",
    "remap_refs",
    "single_key_marker",
    "stack_output_from_marker",
    "transform_from_marker",
]


def single_key_marker(value: Any, key: str) -> Any | None:
    """The payload of a strict single-key ``{key: payload}`` marker, or ``None``.

    Every ``$…`` marker shares this shape: exactly one key, and that key is the
    marker's. Each predicate adds its own payload test on top.
    """
    if isinstance(value, dict) and len(value) == 1:
        return value.get(key)
    return None


def is_ref_marker(value: Any) -> bool:
    """A strict ``{"$ref": "node_id#attr"}`` marker (single key, str value).

    The ``"#"`` is part of the shape: a ``$ref`` value without it is plain data,
    and :func:`ref_from_marker` would raise ``ValueError`` on it.
    """
    target = single_key_marker(value, REF_KEY)
    return isinstance(target, str) and "#" in target


def ref_from_marker(value: dict[str, Any]) -> Ref:
    """Parse a strict ``$ref`` marker back into a :class:`Ref`."""
    node_id, attr = value[REF_KEY].split("#", 1)
    return Ref(node_id, attr)


def is_stack_output_marker(value: Any) -> bool:
    """A strict ``{"$stack_output": "stack:name"}`` marker (single key, str value)."""
    return isinstance(single_key_marker(value, STACK_OUTPUT_KEY), str)


def stack_output_from_marker(value: dict[str, Any]) -> StackOutputRef:
    """Parse a strict ``$stack_output`` marker back into a :class:`StackOutputRef`."""
    stack, name = value[STACK_OUTPUT_KEY].split(":", 1)
    return StackOutputRef(stack, name)


def is_ref_or_marker(value: Any) -> bool:
    """A value that stands in for an upstream output: a ``Ref`` or its marker."""
    return isinstance(value, Ref) or is_ref_marker(value)


def is_transform_marker(value: Any) -> bool:
    """A strict ``{"$transform": {"op": ..., "args": [...]}}`` marker."""
    body = single_key_marker(value, TRANSFORM_KEY)
    return isinstance(body, dict) and "op" in body and "args" in body


def transform_from_marker(value: dict[str, Any]) -> tuple[str, list[Any]]:
    """Parse a ``$transform`` marker into ``(op, args)`` (args stay in marker form)."""
    body = value[TRANSFORM_KEY]
    return body["op"], list(body["args"])


def contains_ref(value: Any) -> bool:
    """True if a live ``Ref`` object occurs anywhere in ``value``."""
    return tree_any(value, lambda v: isinstance(v, Ref))


def contains_handle(value: Any) -> bool:
    """True if any live handle occurs anywhere in ``value``.

    Used by the field validator: any nested handle (e.g. inside a ``tags`` dict)
    defers validation until it resolves at apply.
    """
    return tree_any(value, lambda v: isinstance(v, HANDLES))


def collect_refs(value: Any) -> list[Ref]:
    """Every live ``Ref`` object reachable from ``value`` (traversal order)."""
    return tree_collect(value, lambda v: isinstance(v, Ref))


def has_ref_key(value: Any) -> bool:
    """True if any dict in ``value`` carries a ``"$ref"`` key (canonicalized trees).

    Looser than :func:`is_ref_marker`. Used by the diff on lowered IR/state values,
    which contain no sets.
    """
    return tree_any(value, lambda v: isinstance(v, dict) and REF_KEY in v, include_sets=False)


def collect_ref_targets(value: Any) -> frozenset[str]:
    """Node ids every ``$ref`` key anywhere in ``value`` points at.

    Same loose match as :func:`has_ref_key`. The diff uses it to attribute a
    field's change to the upstream nodes it references.
    """
    found = tree_collect(
        value,
        lambda v: isinstance(v, dict) and isinstance(v.get(REF_KEY), str),
        include_sets=False,
    )
    return frozenset(str(v[REF_KEY]).partition("#")[0] for v in found)


def canonicalize(value: Any) -> Any:
    """Resource-input canonical form: every handle type becomes a marker.

    Matches ``Resource.canonical_inputs`` semantics exactly (keys stringified,
    sets lowered to sorted lists); the bytes feed the IR canonical hash.
    """
    return _tree.handles_to_markers(value, HANDLES, stringify_keys=True)


def remap_refs(value: Any, remap: Mapping[str, str]) -> Any:
    """Rewrite the target node id of every ``$ref`` marker via ``remap``.

    Operates on canonicalized trees (markers, not live handles); migrates
    persisted state when a resource is renamed via ``aliases``. ``tree_map``
    descends into nested markers, so ``$ref``s inside a ``$transform`` are
    rewritten too.
    """

    def leaf(v: Any) -> Any:
        if is_ref_marker(v):
            ref = ref_from_marker(v)
            if ref.node_id in remap:
                return {REF_KEY: f"{remap[ref.node_id]}#{ref.attr}"}
        return v

    return tree_map(value, leaf)


def refs_to_markers(value: Any) -> Any:
    """Artifact-output form: only ``Ref`` and ``Transform`` objects become markers.

    Matches the ``.atlas`` artifact's stored-output semantics exactly (keys
    stringified, sets lowered to sorted lists); other handle types are left as-is.
    """
    return _tree.handles_to_markers(value, (Ref, Transform), stringify_keys=True)
