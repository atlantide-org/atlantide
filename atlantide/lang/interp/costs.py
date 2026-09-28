"""Fuel prices: what evaluation work costs, so halting stays provable.

One step per interpreter node is charged by the evaluator itself. The functions
here price the work a single node can hand to native code — a builtin looping
over its argument, a comparison recursing through a nested value, an operator
whose result is far larger than its inputs — up front, before it runs.
"""

from __future__ import annotations

import ast
import math
import re
import sys
from collections.abc import Collection
from typing import Any

from atlantide.core.errors import FuelExhaustedError, LanguageError

#: Evaluation steps a config may spend unless told otherwise. A resource costs
#: roughly a hundred steps, so this admits configs of ~50k resources while still
#: stopping a runaway loop within seconds. A fixed bound, never scaled to the
#: input: halting is only provable against a number chosen before evaluation.
DEFAULT_FUEL = 5_000_000

#: Largest integer a single operation may produce. Config needs port numbers and
#: byte sizes, not bignums; without a cap `x = x * x` doubles the bit length per
#: tick and reaches gigabytes in thirty steps.
MAX_INT_BITS = 1 << 16

#: Result size (characters or elements) a concatenation may reach before it is
#: charged: building names and paths from parts is ordinary config and must not
#: cost fuel per character, while `x = x + x` doubling is caught long before it
#: matters for memory.
_FREE_CONCAT = 1024

#: Characters (or elements) of output one step of fuel pays for when a native
#: builds a large result: the concatenation rate, applied to padding, joins,
#: replacements, format widths and ``to_bytes``.
_OUTPUT_RATE = 16

_SEQUENCE = (str, bytes, list, tuple)
SETS = (set, frozenset)
_KEYS_VIEW: type[Collection[Any]] = type({}.keys())
_VALUES_VIEW: type[Collection[Any]] = type({}.values())
_ITEMS_VIEW: type[Collection[Any]] = type({}.items())
#: A dict's ``keys()``/``values()``/``items()``: walked (and rendered) as the
#: containers they are.
VIEWS: tuple[type[Collection[Any]], ...] = (_KEYS_VIEW, _VALUES_VIEW, _ITEMS_VIEW)
CONTAINER: tuple[type[Collection[Any]], ...] = (list, tuple, *SETS, *VIEWS)
_TEXT = (str, bytes)
NESTED: tuple[type[Collection[Any]], ...] = (dict, *CONTAINER)
COMPOUND: tuple[type[Collection[Any]], ...] = (*NESTED, *_TEXT)


def _sized_len(value: Any) -> int:
    """``len(value)`` if cheaply known, else 0 (generators/maps have no len)."""
    try:
        return len(value)
    except TypeError:
        return 0
    except OverflowError:
        # `range(10**20)`: too long to even count, so certainly too long to walk.
        return sys.maxsize


def deep_cost(value: Any, limit: int) -> int:
    """Nodes a native walk over ``value`` visits, stopping once past ``limit``.

    Shared subtrees count once per reference, as a serializer, ``repr`` or
    ``==`` traverses them: ``x = [x, x]`` sixty times is 2**60 nodes to
    ``to_json`` even though it is 61 objects in memory. A top-level leaf costs
    one step plus its length (a string, a ``range``: what a native loops over);
    a nested string costs one step per machine word of it, so ordinary tag and
    name dicts stay cheap.
    """
    if not isinstance(value, NESTED):
        return 1 + _sized_len(value)
    total = 0
    stack: list[Any] = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            total += 1 + len(item)
            if total <= limit:
                stack.extend(item.keys())
                stack.extend(item.values())
        elif isinstance(item, CONTAINER):
            total += 1 + len(item)
            if total <= limit:
                stack.extend(item)
        elif isinstance(item, _TEXT):
            total += 1 + len(item) // 64
        else:
            total += 1 + _sized_len(item)
        if total > limit:
            return total
    return total


#: Builtins that answer in constant time whatever their argument: ``len(rows)``
#: reads a stored size, so walking ``rows`` would overcharge it.
_CONSTANT_TIME_BUILTINS: tuple[Any, ...] = (len, bool, isinstance)

#: Methods of a builtin ``list``/``dict`` that run in (amortised) constant time:
#: they neither walk the receiver nor their argument, beyond hashing a key.
#: `expressions` charges a bound method's receiver only outside this set.
CONSTANT_TIME_METHODS: frozenset[str] = frozenset(
    {"append", "get", "setdefault", "pop", "keys", "values", "items"}
)

#: ``str``/``bytes`` methods whose first argument is the width of the result.
_PADDING = frozenset({"ljust", "rjust", "center", "zfill"})


def native_call_cost(args: list[Any], kwargs: dict[str, Any], limit: int, func: Any = None) -> int:
    """Fuel charged for a native builtin call.

    One step per argument plus the nodes a native walk over it would visit, so
    ``sum(range(N))`` costs about N, ``to_json`` of a nested value costs its
    full (unshared) size, and a runaway native loop reaches the fuel limit
    rather than running unbounded. ``limit`` is the remaining fuel: counting
    stops once past it.

    Given ``func``, a constant-time builtin or list/dict method costs a flat
    step per argument instead, and a native whose result can dwarf its inputs
    (padding, ``join``, ``replace``, ``int.to_bytes``) is also charged for the
    output it would build.
    """
    if func is not None and _is_constant_time(func):
        return len(args) + len(kwargs) + _key_cost(func, args, limit)
    total = 0
    for value in [*args, *kwargs.values()]:
        total += deep_cost(value, limit - total)
        if total > limit:
            return total
    if func is not None:
        total += _output_cost(func, args, kwargs)
    return total


def _is_constant_time(func: Any) -> bool:
    if any(func is builtin for builtin in _CONSTANT_TIME_BUILTINS):
        return True
    owner = getattr(func, "__self__", None)
    return type(owner) in (list, dict) and getattr(func, "__name__", None) in CONSTANT_TIME_METHODS


def _key_cost(func: Any, args: list[Any], limit: int) -> int:
    """A dict method hashes its key, which walks a tuple/frozenset natively."""
    owner = getattr(func, "__self__", None)
    if isinstance(owner, dict) and args and isinstance(args[0], tuple | frozenset):
        return deep_cost(args[0], limit)
    return 0


def _size_cost(size: int) -> int:
    """Fuel for building ``size`` characters/elements of output; small results are free."""
    return size // _OUTPUT_RATE if size > _FREE_CONCAT else 0


def _output_cost(func: Any, args: list[Any], kwargs: dict[str, Any]) -> int:
    """Fuel for the result ``func(*args, **kwargs)`` would build, estimated up front."""
    if func is sum:
        start = args[1] if len(args) > 1 else kwargs.get("start")
        if isinstance(start, list | tuple):
            # Each step copies the running total: quadratic, and unpriceable
            # up front for an unsized iterable. Python refuses a `str` start
            # for the same reason.
            raise LanguageError(
                "sum() with a list or tuple start is quadratic; flatten with a "
                "comprehension instead: [x for xs in lists for x in xs]"
            )
        return 0
    name = getattr(func, "__name__", None)
    owner = getattr(func, "__self__", None)
    if owner is None and getattr(func, "__objclass__", None) in (str, bytes, int) and args:
        owner, args = args[0], args[1:]  # `str.ljust(s, n)`: the receiver is the first arg
    if isinstance(owner, str | bytes):
        if name in _PADDING and args and isinstance(args[0], int):
            return _size_cost(args[0])
        if name == "join" and args:
            return _size_cost(_join_size(owner, args[0]))
        if name == "replace" and len(args) >= 2:
            count = args[2] if len(args) > 2 else kwargs.get("count", -1)
            return _size_cost(_replace_bound(owner, args[0], args[1], count))
    elif isinstance(owner, int) and name == "to_bytes":
        length = args[0] if args else kwargs.get("length", 1)
        if isinstance(length, int):
            return _size_cost(length)
    return 0


def _join_size(sep: str | bytes, items: Any) -> int:
    """Length of ``sep.join(items)`` for a sized iterable (already charged per item)."""
    n = _sized_len(items)
    if n == 0 or n == sys.maxsize:
        return 0
    total = sum(len(item) for item in items if isinstance(item, str | bytes))
    return len(sep) * (n - 1) + total


def _replace_bound(text: str | bytes, old: Any, new: Any, count: Any) -> int:
    """An upper bound on ``len(text.replace(old, new, count))``."""
    if not isinstance(old, str | bytes) or not isinstance(new, str | bytes):
        return 0
    grow = len(new) - len(old)
    if grow <= 0:
        return len(text)
    # An empty `old` matches between every character and at both ends.
    matches = len(text) // len(old) + 1 if old else len(text) + 1
    if isinstance(count, int) and count >= 0:
        matches = min(matches, count)
    return len(text) + matches * grow


def _spec_int(digits: str) -> int:
    """A width/precision as written; one too long to parse is certainly too large."""
    return int(digits) if len(digits) <= 18 else sys.maxsize


_DIGITS = re.compile(r"\d+")


def format_cost(spec: str) -> int:
    """Fuel for applying format spec ``spec``: its width and precision size the output.

    Every digit run in the spec is counted, an upper bound on width plus
    precision (a digit fill character adds at most 9). The rendered value
    itself is existing memory, charged where it was built.
    """
    return _size_cost(sum(_spec_int(run) for run in _DIGITS.findall(spec)))


#: A printf-style conversion: its width and precision, each digits or ``*``.
_PRINTF_SPEC = re.compile(r"%(?:\([^)]*\))?[-#0 +]*(\*|\d+)?(?:\.(\*|\d*))?[hlL]?[a-zA-Z%]")


def _printf_widths(template: str, values: Any) -> int:
    """Sum of every width and precision in printf-style ``template``.

    A ``*`` takes its value from ``values``; bounded by the largest integer there.
    """
    star = 0
    candidates = values if isinstance(values, tuple) else (values,)
    ints = [abs(v) for v in candidates if isinstance(v, int)]
    if ints:
        star = max(ints)
    total = 0
    for match in _PRINTF_SPEC.finditer(template):
        for part in match.groups():
            if part == "*":
                total += star
            elif part:
                total += _spec_int(part)
    return total


def _int_result_bits(op_type: type[ast.operator], left: int, right: int) -> int:
    """An upper bound on the bit length of ``left <op> right``, computed without
    performing it."""
    lbits, rbits = left.bit_length(), right.bit_length()
    match op_type:
        case ast.Mult:
            return lbits + rbits
        case ast.LShift:
            return lbits + max(right, 0)
        case ast.Pow:
            if right <= 0 or abs(left) <= 1:
                return 1  # a float, 0, or +-1
            return int(right * math.log2(abs(left))) + 1
        case ast.FloorDiv | ast.Mod | ast.RShift:
            return max(lbits, rbits)
        case _:
            return max(lbits, rbits) + 1  # Add, Sub, BitOr, BitAnd, BitXor


def binop_cost(op_type: type[ast.operator], left: Any, right: Any) -> int:
    """Fuel for a binary op whose result can be far larger than its inputs.

    Covers sequence repetition (``"a" * N``), concatenation (``x + x``) and
    integer arithmetic, bounding output size so one node cannot allocate
    unbounded memory per tick. An integer result past :data:`MAX_INT_BITS` is
    refused outright, before it is computed.
    """
    if isinstance(left, int) and isinstance(right, int):
        bits = _int_result_bits(op_type, left, right)
        if bits > MAX_INT_BITS:
            # A bound on evaluation like fuel, and reported as one.
            raise FuelExhaustedError(
                f"integer result would exceed {MAX_INT_BITS} bits; Atlas-lang integers are bounded"
            )
        return bits // 64
    match op_type:
        case ast.Mult:
            # Normalise to (sequence, count) regardless of operand order.
            seq, count = (left, right) if isinstance(left, _SEQUENCE) else (right, left)
            if isinstance(seq, _SEQUENCE) and isinstance(count, int):
                return _sized_len(seq) * max(count, 0)
        case ast.Mod if isinstance(left, _TEXT):
            # printf-style formatting renders `right`, as str() would; a width
            # or precision (`'%*d' % (10**9, 1)`) pads the result to that size.
            template = left if isinstance(left, str) else left.decode("latin-1")
            return _sized_len(left) + _printf_widths(template, right) // _OUTPUT_RATE
        case ast.Add | ast.Sub | ast.BitOr | ast.BitAnd | ast.BitXor:
            size = _sized_len(left) + _sized_len(right)
            return size // 16 if size > _FREE_CONCAT else 0
    return 0


def compare_cost(op: ast.cmpop, left: Any, right: Any, limit: int) -> int:
    """Fuel for a comparison that recurses natively.

    ``==`` between two separately built nested values walks both, and ``in``
    compares against every element (or hashes a tuple key), so these are charged
    by the nodes involved rather than one step. Text is scanned a machine word
    at a time, so it costs one step per 64 characters.
    """
    if isinstance(op, ast.Is | ast.IsNot):
        return 0
    if isinstance(op, ast.In | ast.NotIn):
        if isinstance(right, dict | set | frozenset | _KEYS_VIEW | _ITEMS_VIEW):
            return deep_cost(left, limit)  # hashing the probe
        if isinstance(right, list | tuple | _VALUES_VIEW):
            return min(len(right) * deep_cost(left, limit), limit + 1)
        if isinstance(left, _TEXT) and isinstance(right, _TEXT):
            return (len(left) + len(right)) // 64  # substring search
        return 0
    if isinstance(left, _TEXT) and isinstance(right, _TEXT):
        return min(len(left), len(right)) // 64
    if isinstance(left, NESTED) and isinstance(right, NESTED):
        return _smaller_deep_cost(left, right, limit)
    return 0


def _smaller_deep_cost(left: Any, right: Any, limit: int) -> int:
    """``min(deep_cost(left), deep_cost(right))``, walking only about as far as the smaller.

    A native ``==`` stops at the first difference, so it never walks further
    than the smaller side; walking both fully would do unbounded work for a
    small charge when one side is a huge shared structure. Both sides are
    walked to a doubling bound until one completes.
    """
    bound = 64
    while True:
        left_cost, right_cost = deep_cost(left, bound), deep_cost(right, bound)
        if left_cost <= bound or right_cost <= bound or bound > limit:
            return min(left_cost, right_cost)
        bound *= 2


def slice_cost(container: Any, key: Any) -> int:
    """Fuel for ``container[key]`` when ``key`` is a slice: the copy it makes.

    Text is copied a machine word at a time (one step per 64 characters); a
    list or tuple slice at the concatenation rate. A ``range`` slices in
    constant time.
    """
    if not isinstance(key, slice) or not isinstance(container, _SEQUENCE):
        return 0
    try:
        size = len(range(*key.indices(len(container))))
    except (TypeError, ValueError):
        return 0  # a bad slice: the native raises
    return size // 64 if isinstance(container, _TEXT) else size // _OUTPUT_RATE
