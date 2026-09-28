"""Generic recursive walks over property-value trees.

Property/IR values are nested containers of scalars (plus ``Ref`` markers). These
primitives back "does any node satisfy P?", "collect every node satisfying P",
and "rebuild the tree transforming its leaves".

``include_sets`` toggles whether ``set``/``frozenset`` are traversed by the
inspection walks: canonicalized IR trees (sets already lowered to sorted lists)
pass ``False``, pre-canonical resource values pass ``True``. :func:`tree_map`
takes no such flag: it always lowers a set to a sorted list.

:func:`handles_to_markers` is the ``tree_map`` every handle-to-marker conversion
in ``core.types`` and ``core.markers`` shares; it lives here, in a private
module, so it is not config API.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator
from typing import Any, cast

from atlantide.core.errors import IRError

_SEQ = (list, tuple)
_SEQ_WITH_SETS = (list, tuple, set, frozenset)
#: Exact leaf types with no children, checked by ``type(v) in`` before any
#: ``isinstance`` chain: most nodes in a property tree are scalars.
_SCALARS = frozenset({str, int, float, bool, type(None)})


def _children(value: Any, *, include_sets: bool) -> Iterable[Any] | None:
    """What a walker descends into, or ``None`` when ``value`` is a leaf.

    Shared by :func:`tree_any` and :func:`tree_collect`. Containers are mappings,
    sequences, live handles carrying nested values (a ``Transform`` exposes
    ``_atlas_operands``), and nested pydantic models. Models are traversed so a
    ``Ref`` inside a structured field (a route's gateway) still forms its
    dependency edge. They are duck-typed so this low-level module does not
    import pydantic.
    """
    if type(value) in _SCALARS:
        return None
    if isinstance(value, dict):
        return value.values()
    if isinstance(value, _SEQ_WITH_SETS if include_sets else _SEQ):
        return value
    operands: tuple[Any, ...] | None = getattr(value, "_atlas_operands", None)
    if operands is not None:
        return operands
    fields = _model_dict(value)
    return None if fields is None else fields.values()


def tree_any(value: Any, predicate: Callable[[Any], bool], *, include_sets: bool = True) -> bool:
    """True if ``predicate`` holds for ``value`` or any nested element."""
    if predicate(value):
        return True
    if type(value) in _SCALARS:
        return False
    children = _children(value, include_sets=include_sets)
    return children is not None and any(
        tree_any(child, predicate, include_sets=include_sets) for child in children
    )


def tree_collect(
    value: Any, predicate: Callable[[Any], bool], *, include_sets: bool = True
) -> list[Any]:
    """Every node (in traversal order) for which ``predicate`` holds."""
    found: list[Any] = []
    _collect(value, predicate, found, include_sets=include_sets)
    return found


def _collect(
    value: Any, predicate: Callable[[Any], bool], out: list[Any], *, include_sets: bool
) -> None:
    """A matching node is collected whole; the walk does not descend into it."""
    if predicate(value):
        out.append(value)
        return
    for child in _children(value, include_sets=include_sets) or ():
        _collect(child, predicate, out, include_sets=include_sets)


def _model_dict(value: Any) -> dict[str, Any] | None:
    """A nested pydantic model's fields as a mapping, or ``None`` if not one.

    The model's own ``__dict__``, not a copy: callers only iterate it.
    """
    if getattr(value, "__pydantic_fields_set__", None) is None:
        return None
    fields = getattr(value, "__dict__", None)
    return fields if isinstance(fields, dict) else None


def order_key(value: Any) -> str:
    """Sort key giving a total order over already-mapped tree values.

    A set's elements pass through ``leaf`` before it is lowered, so a set of
    handles becomes a list of dicts, which ``sorted`` cannot compare directly.
    """
    return json.dumps(value, sort_keys=True, default=repr)


def _mapped_keys(value: dict[Any, Any], *, stringify: bool) -> Iterator[tuple[Any, Any]]:
    """Yield ``(stored_key, original_key)``, rejecting a stringify collision.

    ``str`` is not injective (``1`` and ``"1"``, ``True`` and ``"True"``), and a
    collision would drop a property from the IR, the hash, and the provider call.
    """
    seen: dict[Any, Any] = {}
    for key in value:
        stored = str(key) if stringify else key
        if stored in seen:
            raise IRError(
                f"property keys {seen[stored]!r} and {key!r} both encode as {stored!r}; "
                "canonical keys must be distinct strings"
            )
        seen[stored] = key
        yield stored, key


def tree_map(value: Any, leaf: Callable[[Any], Any], *, stringify_keys: bool = False) -> Any:
    """Rebuild ``value`` applying ``leaf`` to every node, recursing into containers.

    ``leaf`` runs on the whole value first: return a replacement to stop, or the
    value unchanged to descend into its container. ``stringify_keys`` coerces dict
    keys to ``str``, rejecting a collision.

    A set is lowered to a list ordered by :func:`order_key`: a set is not
    JSON-serializable, and its iteration order varies with ``PYTHONHASHSEED``.
    """
    replaced = leaf(value)
    if replaced is not value or type(value) in _SCALARS:
        return replaced

    def recur(item: Any) -> Any:
        return tree_map(item, leaf, stringify_keys=stringify_keys)

    if isinstance(value, dict):
        return {
            stored: recur(value[key])
            for stored, key in _mapped_keys(value, stringify=stringify_keys)
        }
    if isinstance(value, (set, frozenset)):
        return sorted((recur(item) for item in value), key=order_key)
    if isinstance(value, _SEQ):
        return [recur(item) for item in value]
    fields = _model_dict(value)
    if fields is not None:
        # Lower a nested model to a mapping so `leaf` reaches the handles inside
        # it; otherwise a live `Ref` would reach the canonical form unconverted.
        return {key: recur(item) for key, item in sorted(fields.items())}
    return value


def handles_to_markers(value: Any, kinds: tuple[type, ...], *, stringify_keys: bool = False) -> Any:
    """Rebuild ``value`` with every ``kinds`` handle replaced by its ``canonical()`` marker.

    Shared by every handle-to-marker conversion (``core.types`` and
    ``core.markers``); call sites differ only in which handle types convert and
    whether dict keys are stringified (``stringify_keys`` also lowers sets to
    sorted lists, as in :func:`tree_map`). Every type in ``kinds`` must define
    ``canonical()``; the handles are duck-typed so this module does not import
    ``core.types`` (which imports it).
    """

    def leaf(v: Any) -> Any:
        if isinstance(v, kinds):
            # `kinds` is typed tuple[type, ...], so mypy narrows `v` to `object`.
            return cast("Any", v).canonical()
        return v

    return tree_map(value, leaf, stringify_keys=stringify_keys)
