"""Deterministic text for config values.

Anything config renders can flow into a resource field and from there into the
hashed IR, so the text must not depend on the run. Two things would make it:

* a memory address, embedded in the default ``repr`` of a function or object —
  refused outright (:func:`require_stable_repr`);
* a set's iteration order, which for strings follows ``PYTHONHASHSEED`` — at
  any depth: ``f"{[s]}"`` renders ``s`` as surely as ``str(s)`` does.

:func:`stable_form` is the one renderer every text-producing path goes through
(``str``, f-strings, ``%``): it hands Python the value with each set replaced by
a stand-in listing the elements in a fixed order, so everything else still
renders exactly as Python's own ``repr``/``str``/``format`` would. The order is
the one set iteration uses (`_Evaluator._sorted_set`): elements sorted by their
canonical ``repr`` (:func:`stable_repr`).
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Any, override

from atlantide.core._describe import ADDRESS, describe_type
from atlantide.core.errors import LanguageError
from atlantide.lang.interp.costs import COMPOUND, CONTAINER, SETS, VIEWS

#: Types rendered natively with no walk: nothing inside them to canonicalize.
_SCALARS = frozenset({str, int, float, bool, type(None), bytes})
_UNORDERED: tuple[type[Collection[Any]], ...] = (*SETS, *VIEWS)


def _is_data(value: Any) -> bool:
    return value is None or isinstance(value, (int, float, *COMPOUND))


def _check(value: Any) -> bool:
    """Refuse ``value`` if its text would embed an address; say whether it holds
    anything :func:`stable_form` must stand in for (a set or a dict view)."""
    unordered = False
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, CONTAINER):
            unordered = unordered or isinstance(item, _UNORDERED)
            stack.extend(item)
        elif not _is_data(item) and ADDRESS.search(repr(item)):
            raise LanguageError(
                f"cannot render a {describe_type(item)} as text: its representation "
                "depends on memory layout, so the result would differ between runs"
            )
    return unordered


def require_stable_repr(value: Any) -> None:
    """Reject rendering a value whose text embeds a memory address.

    ``f"{atlantide!r}"``, ``str(to_json)`` or a closure in a list would put
    ``<... at 0x10a2b3c40>`` into a resource field, so the IR hash would differ
    on every run. Callers charge ``deep_cost`` first, so this walk is bounded.
    """
    _check(value)


class _Text:
    """Stands in for an unordered value while Python renders what holds it.

    Its text is fixed at construction; hashing and equality follow the value it
    replaces, so a stand-in works as the dict key or tuple member it replaced.
    """

    __slots__ = ("_text", "_value")

    def __init__(self, value: Any, text: str) -> None:
        self._value = value
        self._text = text

    @override
    def __repr__(self) -> str:
        return self._text

    @override
    def __str__(self) -> str:
        return self._text

    @override
    def __format__(self, spec: str) -> str:
        # As `object.__format__`, which the real set/view uses: a spec is an error.
        if spec:
            raise TypeError(f"unsupported format string passed to {type(self).__name__}.__format__")
        return self._text

    @override
    def __hash__(self) -> int:
        return hash(self._value)

    @override
    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Text) and self._value == other._value


#: One stand-in class per replaced type, named after it, so an error about the
#: stand-in (``'%d' % (s,)``) names the type config actually used.
_STAND_INS: dict[type, type[_Text]] = {
    kind: type(kind.__name__, (_Text,), {"__slots__": ()}) for kind in _UNORDERED
}


def _stand_in(value: Any) -> Any:
    """``value`` rebuilt with every set and dict view replaced by a `_Text`.

    Only called on a value `_check` found one in, and bounded like it: the
    caller has charged fuel for a walk over ``value``.
    """
    if isinstance(value, SETS):
        items = ", ".join(sorted(repr(_stand_in(item)) for item in value))
        kind = set if isinstance(value, set) else frozenset
        if not items:
            text = f"{kind.__name__}()"
        else:
            text = f"{{{items}}}" if kind is set else f"frozenset({{{items}}})"
        return _STAND_INS[kind](value, text)
    if isinstance(value, VIEWS):
        inner = repr([_stand_in(item) for item in value])
        return _STAND_INS[type(value)](value, f"{type(value).__name__}({inner})")
    if isinstance(value, dict):
        return {_stand_in(k): _stand_in(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_stand_in(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_stand_in(item) for item in value)
    return value


def stable_form(value: Any) -> Any:
    """``value`` as it must be handed to ``str``/``repr``/``ascii``/``format``/``%``.

    ``value`` itself when it holds no set or dict view, so data without one
    renders exactly as Python renders it; otherwise a copy in which each renders
    its elements in `stable_repr` order. Refuses a value whose text would embed
    a memory address. The caller charges fuel for the walk first.
    """
    if type(value) in _SCALARS:
        return value
    return _stand_in(value) if _check(value) else value


def holds_set(value: Any) -> bool:
    """Whether a set or frozenset sits anywhere in ``value``'s dicts, lists and tuples."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, SETS):
            return True
        if isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list | tuple):
            stack.extend(item)
    return False


def stable_repr(value: Any) -> str:
    """``repr(value)`` with every set's elements in order: the sort key for set
    elements, so a set iterates in the order it renders."""
    return repr(stable_form(value))
