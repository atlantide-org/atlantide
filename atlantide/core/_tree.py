"""Generic recursive walks over property-value trees.

Property/IR values are nested containers of scalars (plus ``Ref`` markers). These
primitives back "does any node satisfy P?", "collect every node satisfying P",
and "rebuild the tree transforming its leaves".

``include_sets`` toggles whether ``set``/``frozenset`` are traversed by the
inspection walks: canonicalized IR trees (sets already lowered to sorted lists)
pass ``False``, pre-canonical resource values pass ``True``. :func:`tree_map`
takes no such flag — it always lowers a set to a sorted list.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from atlantide.core.errors import IRError

_SEQ = (list, tuple)
_SEQ_WITH_SETS = (list, tuple, set, frozenset)


def _children(value: Any, *, include_sets: bool) -> Iterable[Any] | None:
    """What a walker should descend into, or ``None`` when ``value`` is a leaf.

    The inspection walks (:func:`tree_any`, :func:`tree_collect`) differ only in
    what they do at each node, so the question of *what counts as a child* is
    asked once here. Four kinds of container answer it: mappings, sequences, live
    handles carrying nested values (a ``Transform`` exposes ``_atlas_operands``),
    and nested pydantic models.

    That last one matters more than it looks. Structured resource fields — a
    security-group rule, a route — are models rather than dicts so they can be
    typed and validated, and a walker that stopped at the model boundary would
    silently drop the ``Ref`` inside, which is the edge making a route depend on
    the gateway it points at.

    Models are duck-typed rather than imported: ``core._tree`` is the lowest layer
    in the package and has no dependencies of its own.
    """
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
    children = _children(value, include_sets=include_sets)
    return children is not None and any(
        tree_any(child, predicate, include_sets=include_sets) for child in children
    )


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
    """A matching node is collected whole; the walk does not descend into it."""
    if predicate(value):
        out.append(value)
        return
    for child in _children(value, include_sets=include_sets) or ():
        _collect(child, predicate, out, include_sets)


def _model_dict(value: Any) -> dict[str, Any] | None:
    """A nested pydantic model's fields as a mapping, or ``None`` if not one."""
    if getattr(value, "__pydantic_fields_set__", None) is None:
        return None
    fields = getattr(value, "__dict__", None)
    return dict(fields) if isinstance(fields, dict) else None


def order_key(value: Any) -> str:
    """Sort key giving a total order over already-mapped tree values.

    ``sorted`` alone is not enough: a set's elements have been through ``leaf`` by
    the time it is lowered, so a set of handles is a list of dicts, which are not
    comparable.
    """
    return json.dumps(value, sort_keys=True, default=repr)


def _mapped_keys(value: dict[Any, Any], stringify: bool) -> Iterator[tuple[Any, Any]]:
    """Yield ``(stored_key, original_key)``, rejecting a stringify collision.

    ``str`` is not injective (``1`` and ``"1"``, ``True`` and ``"True"``) and a
    dict comprehension keeps the last writer, which would drop a property from
    the IR, the hash, and the provider call.
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

    A set is lowered to a list ordered by :func:`order_key`. Emitting a set is
    never correct: it is not JSON-serializable, and its iteration order varies
    with ``PYTHONHASHSEED``, which would carry into every hash derived from it.
    """
    replaced = leaf(value)
    if replaced is not value:
        return replaced

    def recur(item: Any) -> Any:
        return tree_map(item, leaf, stringify_keys=stringify_keys)

    if isinstance(value, dict):
        return {stored: recur(value[key]) for stored, key in _mapped_keys(value, stringify_keys)}
    if isinstance(value, (set, frozenset)):
        return sorted((recur(item) for item in value), key=order_key)
    if isinstance(value, _SEQ):
        return [recur(item) for item in value]
    fields = _model_dict(value)
    if fields is not None:
        # A nested model lowers to the mapping it describes, with every handle
        # inside it mapped first. Leaving the model object intact would carry a
        # live `Ref` into the canonical form, where it serializes to whatever
        # pydantic makes of it rather than to the `$ref` marker the rest of the
        # pipeline expects.
        return {key: recur(item) for key, item in sorted(fields.items())}
    return value
