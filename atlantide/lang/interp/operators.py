"""The AST operator tables, and which natives call back into config."""

from __future__ import annotations

import ast
import operator
from collections.abc import Callable
from typing import Any, Literal

BINOPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.LShift: operator.lshift,
    ast.RShift: operator.rshift,
    ast.BitOr: operator.or_,
    ast.BitAnd: operator.and_,
    ast.BitXor: operator.xor,
}

UNARYOPS: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
    ast.Invert: operator.invert,
    ast.Not: operator.not_,
}

#: f-string conversions, keyed by the code `ast` records (`!r`, `!a`, `!s`).
#: `-1` (no conversion) is absent.
CONVERSIONS: dict[int, Callable[[Any], str]] = {
    ord("r"): repr,
    ord("a"): ascii,
    ord("s"): str,
}

COMPARES: dict[type[ast.cmpop], Callable[[Any, Any], Any]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}

#: Where a native takes the callable it invokes: positional index 0, or ``key=``.
type CallbackSlot = Literal[0, "key"]


def callback_slot(func: Any) -> CallbackSlot | None:
    """The argument native ``func`` calls back through, or ``None`` if it calls none.

    ``map(f, ...)``/``filter(f, ...)`` take it first; ``sorted``, ``min``,
    ``max`` and ``xs.sort`` only as ``key=`` (keyword-only in each). Every other
    argument of these is a value: ``max``'s ``default=`` is returned as given.
    Tested by identity rather than a table lookup, since this runs on every
    native call.
    """
    if func is map or func is filter:
        return 0
    if func is sorted or func is min or func is max:
        return "key"
    if getattr(func, "__name__", None) == "sort" and isinstance(
        getattr(func, "__self__", None), list
    ):
        return "key"
    return None
