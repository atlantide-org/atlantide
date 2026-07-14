"""The Ref/marker codec: one home for handle <-> ``{"$...": ...}`` conversions.

Live handle objects (:class:`~atlantide.core.types.Ref`,
:class:`~atlantide.core.types.SecretRef`,
:class:`~atlantide.core.types.StackOutputRef`) serialize to single-key dict
*markers* via their ``canonical()`` methods; IR, state, and artifacts carry the
markers. This module owns the constants, the strict per-marker predicates and
parsers, and the tree-level conversions.

Three distinct ref-detection predicates answer different questions:

- :func:`contains_ref` — a live ``Ref`` *object* anywhere (pre-lowering values);
- :func:`has_ref_key` — a dict with a ``"$ref"`` key anywhere (canonicalized
  IR/state trees, loose match used by the diff);
- :func:`is_ref_or_marker` — a single value that stands in for an upstream
  output, in either form (the executor's resolution test).

``$secret_ref`` markers are owned by :mod:`atlantide.secrets` (import
``is_secret_ref_marker``/``secret_ref_from_marker`` from there).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from atlantide.core._tree import tree_any, tree_collect, tree_map
from atlantide.core.types import HANDLES, Ref, StackOutputRef, Transform

REF_KEY = "$ref"
STACK_OUTPUT_KEY = "$stack_output"
TRANSFORM_KEY = "$transform"


def is_ref_marker(value: Any) -> bool:
    """A strict ``{"$ref": "node_id#attr"}`` marker (single key, str value)."""
    return isinstance(value, dict) and isinstance(value.get(REF_KEY), str) and len(value) == 1


def ref_from_marker(value: dict[str, Any]) -> Ref:
    """Parse a strict ``$ref`` marker back into a :class:`Ref`."""
    node_id, attr = value[REF_KEY].split("#", 1)
    return Ref(node_id, attr)


def is_stack_output_marker(value: Any) -> bool:
    """A strict ``{"$stack_output": "stack:name"}`` marker (single key, str value)."""
    return (
        isinstance(value, dict)
        and isinstance(value.get(STACK_OUTPUT_KEY), str)
        and len(value) == 1
    )


def stack_output_from_marker(value: dict[str, Any]) -> StackOutputRef:
    """Parse a strict ``$stack_output`` marker back into a :class:`StackOutputRef`."""
    stack, name = value[STACK_OUTPUT_KEY].split(":", 1)
    return StackOutputRef(stack, name)


def is_ref_or_marker(value: Any) -> bool:
    """A value that stands in for an upstream output: a ``Ref`` or its marker."""
    return isinstance(value, Ref) or is_ref_marker(value)


def is_transform_marker(value: Any) -> bool:
    """A strict ``{"$transform": {"op": ..., "args": [...]}}`` marker."""
    return (
        isinstance(value, dict)
        and len(value) == 1
        and isinstance(value.get(TRANSFORM_KEY), dict)
        and "op" in value[TRANSFORM_KEY]
        and "args" in value[TRANSFORM_KEY]
    )


def transform_from_marker(value: dict[str, Any]) -> tuple[str, list[Any]]:
    """Parse a ``$transform`` marker into ``(op, args)`` (args stay in marker form)."""
    body = value[TRANSFORM_KEY]
    return body["op"], list(body["args"])


def contains_ref(value: Any) -> bool:
    """True if a live ``Ref`` object occurs anywhere in ``value``."""
    return tree_any(value, lambda v: isinstance(v, Ref))


def collect_refs(value: Any) -> list[Ref]:
    """Every live ``Ref`` object reachable from ``value`` (traversal order)."""
    return tree_collect(value, lambda v: isinstance(v, Ref))


def has_ref_key(value: Any) -> bool:
    """True if any dict in ``value`` carries a ``"$ref"`` key (canonicalized trees).

    Looser than :func:`is_ref_marker`: the diff walks already-lowered IR/state
    values, where sets are gone and a ``$ref`` key is decisive.
    """
    return tree_any(value, lambda v: isinstance(v, dict) and REF_KEY in v, include_sets=False)


def canonicalize(value: Any) -> Any:
    """Resource-input canonical form: every handle type becomes a marker.

    Matches ``Resource.canonical_inputs`` semantics exactly (keys stringified,
    sets lowered to sorted lists) — the bytes feed the IR canonical hash.
    """
    return tree_map(
        value,
        lambda v: v.canonical() if isinstance(v, HANDLES) else v,
        stringify_keys=True,
        sort_sets=True,
    )


def remap_refs(value: Any, remap: Mapping[str, str]) -> Any:
    """Rewrite the target node id of every ``$ref`` marker via ``remap``.

    Operates on already-canonicalized trees (markers, not live handles) — used to
    migrate persisted state when a resource is renamed via ``aliases``. Nested
    markers (e.g. a ``$transform`` carrying ``$ref``s) are reached because
    ``tree_map`` descends into their dicts.
    """

    def leaf(v: Any) -> Any:
        if is_ref_marker(v):
            ref = ref_from_marker(v)
            if ref.node_id in remap:
                return {REF_KEY: f"{remap[ref.node_id]}#{ref.attr}"}
        return v

    return tree_map(value, leaf, include_sets=False)


def refs_to_markers(value: Any) -> Any:
    """Artifact-output form: only ``Ref`` objects become markers.

    Matches the ``.atlas`` artifact's stored-output semantics exactly (sets not
    traversed, keys stringified); other handle types are left as-is.
    """
    return tree_map(
        value,
        lambda v: v.canonical() if isinstance(v, Ref | Transform) else v,
        include_sets=False,
        stringify_keys=True,
    )
