"""Generic recursive walks over property-value trees.

Property/IR values are nested containers of scalars (plus ``Ref`` markers). These
primitives back "does any node satisfy P?", "collect every node satisfying P",
and "rebuild the tree transforming its leaves".

``include_sets`` toggles whether ``set``/``frozenset`` are traversed: canonicalized
IR trees (sets lowered to sorted lists) pass ``False``, pre-canonical resource
values pass ``True``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

_SEQ = (list, tuple)
_SEQ_WITH_SETS = (list, tuple, set, frozenset)


def _operands(value: Any) -> tuple[Any, ...] | None:
    """Children of a live handle that carries nested values (e.g. a ``Transform``).

    Handles opt in by exposing an ``_atlas_operands`` tuple; scalars and ordinary
    containers return ``None`` here (they are traversed by the branches above).
    """
    return getattr(value, "_atlas_operands", None)


def tree_any(value: Any, predicate: Callable[[Any], bool], *, include_sets: bool = True) -> bool:
    """True if ``predicate`` holds for ``value`` or any nested element."""
    if predicate(value):
        return True
    if isinstance(value, dict):
        return any(tree_any(v, predicate, include_sets=include_sets) for v in value.values())
    if isinstance(value, _SEQ_WITH_SETS if include_sets else _SEQ):
        return any(tree_any(v, predicate, include_sets=include_sets) for v in value)
    operands = _operands(value)
    if operands is not None:
        return any(tree_any(v, predicate, include_sets=include_sets) for v in operands)
    return False


def tree_collect(
    value: Any, predicate: Callable[[Any], bool], *, include_sets: bool = True
) -> list[Any]:
    """Every node (in traversal order) for which ``predicate`` holds."""
    found: list[Any] = []
    _collect(value, predicate, found, include_sets)
    return found


def _collect(
    value: Any, predicate: Callable[[Any], bool], out: list[Any], include_sets: bool
) -> None:
    if predicate(value):
        out.append(value)
        return
    if isinstance(value, dict):
        for v in value.values():
            _collect(v, predicate, out, include_sets)
    elif isinstance(value, _SEQ_WITH_SETS if include_sets else _SEQ):
        for v in value:
            _collect(v, predicate, out, include_sets)
    else:
        operands = _operands(value)
        if operands is not None:
            for v in operands:
                _collect(v, predicate, out, include_sets)


def tree_map(
    value: Any,
    leaf: Callable[[Any], Any],
    *,
    include_sets: bool = True,
    stringify_keys: bool = False,
    sort_sets: bool = False,
) -> Any:
    """Rebuild ``value`` applying ``leaf`` to every node, recursing into containers.

    ``leaf`` runs on the whole value first: return a replacement to stop, or the
    value unchanged to descend into its container. ``stringify_keys`` coerces dict
    keys to ``str``; ``sort_sets`` lowers ``set``/``frozenset`` to a sorted list.
    """
    replaced = leaf(value)
    if replaced is not value:
        return replaced
    if isinstance(value, dict):
        key = str if stringify_keys else (lambda k: k)
        return {
            key(k): tree_map(v, leaf, include_sets=include_sets,
                             stringify_keys=stringify_keys, sort_sets=sort_sets)
            for k, v in value.items()
        }
    if isinstance(value, (set, frozenset)) and (include_sets or sort_sets):
        mapped = [tree_map(v, leaf, include_sets=include_sets,
                           stringify_keys=stringify_keys, sort_sets=sort_sets) for v in value]
        return sorted(mapped) if sort_sets else mapped
    if isinstance(value, _SEQ):
        return [tree_map(v, leaf, include_sets=include_sets,
                         stringify_keys=stringify_keys, sort_sets=sort_sets) for v in value]
    return value
