"""Showing a config value in an error message without leaking the engine.

An error is read by a person and compared across runs, so the text must be
stable and must say something about the value in the author's own terms. A raw
``repr`` does neither for anything but plain data: a function's repr embeds a
memory address (different on every run), and the interpreter's own objects
(``Closure``, ``Scope``) would print their internals.

:func:`describe_value` renders plain data exactly as ``repr`` does, and anything
else as a short stable token:
``<lambda>``, ``<function f>``, ``<class 'str'>`` or ``<S3Bucket object>``.
:func:`describe_type` is the matching type word for "got a ..." phrasing.
:func:`scrub_addresses` is the fallback for text that was already rendered
elsewhere, such as a native exception's message.
"""

from __future__ import annotations

import re
from typing import Any

#: What the default ``object.__repr__`` (and function/method reprs) embed: a
#: memory address, different on every run.
ADDRESS = re.compile(r" at 0x[0-9a-fA-F]+>")

_SCALARS = (type(None), bool, int, float, complex, str, bytes)
_SEQUENCES = (list, tuple, set, frozenset)

#: Limits for :func:`describe_value`: values visited, nesting depth, and output
#: length in characters.
_MAX_NODES = 64
_MAX_DEPTH = 6
_MAX_CHARS = 200
_ELLIPSIS = "..."


def describe_value(value: Any) -> str:
    """``value`` as an error message shows it.

    Plain data (``None``, numbers, strings, bytes, and lists/tuples/sets/dicts
    of them) is its ``repr``, cut at a bounded length. A function is ``<lambda>``
    or ``<function name>``, a class ``<class 'Name'>``, and any other object
    ``<TypeName object>``; containers holding one render the rest as data.
    """
    text = repr(value) if is_plain_data(value) else _render(value, [_MAX_NODES], 0)
    return text if len(text) <= _MAX_CHARS else text[:_MAX_CHARS] + _ELLIPSIS


def describe_type(value: Any) -> str:
    """The type word for ``value``: ``type(value).__name__``, except a function
    is ``function`` and a class is ``type`` whatever implements it."""
    if isinstance(value, type):
        return "type"
    if callable(value) and not isinstance(value, _SCALARS):
        return "function"
    return type(value).__name__


def is_plain_data(value: Any) -> bool:
    """Whether ``value`` is small plain data whose ``repr`` is safe to show."""
    stack = [value]
    seen = 0
    while stack:
        item = stack.pop()
        seen += 1
        if seen > _MAX_NODES:
            return False
        if isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, _SEQUENCES):
            stack.extend(item)
        elif not isinstance(item, _SCALARS):
            return False
    return True


def scrub_addresses(text: str) -> str:
    """``text`` with every ``<... at 0x...>`` address dropped, leaving ``<...>``."""
    return ADDRESS.sub(">", text)


def _render(value: Any, budget: list[int], depth: int) -> str:
    """A ``repr``-shaped rendering that spends ``budget`` and stops at ``_MAX_DEPTH``."""
    budget[0] -= 1
    if budget[0] < 0 or depth > _MAX_DEPTH:
        return _ELLIPSIS
    if isinstance(value, _SCALARS):
        return repr(value)
    if isinstance(value, dict):
        entries = _bounded(value.items(), budget)
        parts = [
            f"{_render(key, budget, depth + 1)}: {_render(item, budget, depth + 1)}"
            for key, item in entries
        ]
        return "{" + ", ".join(_elide(parts, cut=len(entries) < len(value))) + "}"
    if isinstance(value, _SEQUENCES):
        items = _bounded(value, budget)
        parts = _elide(
            [_render(item, budget, depth + 1) for item in items], cut=len(items) < len(value)
        )
        return _bracket(value, parts)
    return _describe_object(value)


def _bounded(items: Any, budget: list[int]) -> list[Any]:
    """The first of ``items``, as many as ``budget`` still allows."""
    taken: list[Any] = []
    for item in items:
        if len(taken) >= budget[0]:
            break
        taken.append(item)
    return taken


def _elide(parts: list[str], *, cut: bool) -> list[str]:
    return [*parts, _ELLIPSIS] if cut else parts


def _bracket(value: Any, parts: list[str]) -> str:
    joined = ", ".join(parts)
    if isinstance(value, list):
        return f"[{joined}]"
    if isinstance(value, tuple):
        return f"({joined},)" if len(parts) == 1 else f"({joined})"
    if not parts:
        return f"{type(value).__name__}()"
    return f"{{{joined}}}" if isinstance(value, set) else f"{type(value).__name__}({{{joined}}})"


def _describe_object(value: Any) -> str:
    if isinstance(value, type):
        return f"<class {value.__name__!r}>"
    if callable(value):
        name = getattr(value, "__name__", None)
        if name == "<lambda>":
            return "<lambda>"
        return f"<function {name}>" if isinstance(name, str) else "<function>"
    return f"<{type(value).__name__} object>"
