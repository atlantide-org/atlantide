"""Expression handlers: one ``_ex_<NodeType>`` method per permitted expression.

Also the runtime half of the attribute policy (:func:`_attribute_allowed`),
which is applied where an attribute is actually read.
"""

from __future__ import annotations

import ast
import contextlib
import functools
import types
from collections.abc import Iterable
from typing import Any, override

from pydantic import BaseModel

from atlantide.core._describe import describe_type
from atlantide.core.errors import LanguageError
from atlantide.lang.interp.costs import (
    CONSTANT_TIME_METHODS,
    compare_cost,
    deep_cost,
    format_cost,
    native_call_cost,
)
from atlantide.lang.interp.evaluator import _Evaluator
from atlantide.lang.interp.operators import COMPARES, CONVERSIONS, UNARYOPS, callback_slot
from atlantide.lang.interp.scope import UNBOUND, Closure, Scope
from atlantide.lang.interp.stability import holds_set, stable_form
from atlantide.lang.validate import attribute_rejection

#: Attributes of pydantic's own model API: `parse_file` reads the disk,
#: `model_construct`/`model_copy` bypass validation, `model_fields` reaches the
#: field machinery. A resource exposes its declared fields, never these.
_MODEL_API: frozenset[str] = frozenset(n for n in dir(BaseModel) if not n.startswith("_"))

#: The unbound method, which config reaches as ``set.pop(s)``.
_SET_POP = set.pop

#: Interpreter internals whose *public* attributes lead out of the sandbox: a
#: generator's ``gi_frame``, a frame's ``f_globals``/``f_builtins``/``f_back``, a
#: traceback's ``tb_frame``, a code object's constants, a cell's contents.
_INTERNAL_TYPES: tuple[type, ...] = (
    types.GeneratorType,
    types.CoroutineType,
    types.AsyncGeneratorType,
    types.FrameType,
    types.CodeType,
    types.TracebackType,
    types.CellType,
)

#: What an attribute read must never hand config: an internal above, or a module
#: (whose namespace holds the stdlib modules it imports).
_UNREACHABLE_TYPES: tuple[type, ...] = (types.ModuleType, *_INTERNAL_TYPES)

#: ``@contextmanager`` objects (``region(...)``) keep the generator, the function
#: and its arguments under public names; config only enters them with ``with``.
_GENERATOR_CM: type = getattr(contextlib, "_GeneratorContextManagerBase")  # noqa: B009
_GENERATOR_CM_STATE: frozenset[str] = frozenset({"gen", "func", "args", "kwds"})

#: Methods of a built-in container or string whose cost does not grow with the
#: receiver: charged by their arguments alone. Any other bound method of one
#: (``copy``, ``upper``, ``index``, ``count``, ...) walks its receiver natively.
#: `CONSTANT_TIME_METHODS` (flat-priced in `costs`) plus the methods that are
#: priced by their argument but never walk the receiver: ``s.add(x)`` in a loop
#: must not cost quadratic fuel.
_RECEIVER_FREE: frozenset[str] = CONSTANT_TIME_METHODS | {
    "__getitem__",
    "__len__",
    "add",
    "discard",
    "extend",
    "popitem",
    "update",
}
_RECEIVERS: tuple[type, ...] = (list, dict, set, frozenset, str, bytes, tuple)

#: Items of an unsized star-argument expanded between fuel checks.
_STAR_CHUNK = 1024


def _attribute_allowed(obj: Any, name: str) -> bool:
    """The runtime half of the attribute policy (`validate` is the static half).

    Denied: anything leading-underscore (private state such as
    ``atlantide._inputs``), every attribute of a user function (its ``interp``
    and ``scope`` are the evaluator itself), of a scope, of the evaluator
    (every interpreter object is an ``_Evaluator``) and of a `_NativeCallback`
    (which holds it), every attribute of an interpreter internal (generator,
    frame, code, ...), the generator state of a ``@contextmanager`` object,
    and pydantic's model API on a resource or nested model unless the concrete
    model declares a field of that name.
    """
    if name.startswith("_") or isinstance(obj, Closure | Scope | _Evaluator | _NativeCallback):
        return False
    if isinstance(obj, _INTERNAL_TYPES):
        return False
    if isinstance(obj, _GENERATOR_CM) and name in _GENERATOR_CM_STATE:
        return False
    model = obj if isinstance(obj, type) else type(obj)
    if isinstance(model, type) and issubclass(model, BaseModel) and name in _MODEL_API:
        return name in model.model_fields
    return True


class _NativeCallback:
    """A native callable passed to a higher-order native, called via the interpreter.

    Wraps e.g. ``str`` in ``map(str, xs)`` or ``len`` in ``sorted(xs, key=len)``.
    A native call would skip the checks the same call written in config gets:
    ``str`` would render ``<function f at 0x...>`` unchecked, a set argument
    would iterate in hash-seed order, and the work would cost no fuel. Each call
    goes through :meth:`ExpressionsMixin._call_native` instead.

    It holds the evaluator, so config must never hold it. It is placed only in
    the argument a native calls (`operators.callback_slot`), which no native
    returns: ``max([], default=str)`` returns ``str`` itself. As defence in
    depth, its repr embeds an address (so `stability` refuses to render it) and
    `_attribute_allowed` refuses every attribute of it.
    """

    __slots__ = ("_evaluator", "_func")

    def __init__(self, evaluator: ExpressionsMixin, func: Any) -> None:
        self._evaluator = evaluator
        self._func = func

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._evaluator._call_native(self._func, list(args), kwargs)

    @override
    def __repr__(self) -> str:
        return f"<native callback at {id(self):#x}>"


class ExpressionsMixin(_Evaluator):
    """The ``_ex_*`` handlers. Declares no fields: state lives on `_Evaluator`."""

    def _ex_Constant(self, node: ast.Constant, scope: Scope) -> Any:
        return node.value

    def _ex_Name(self, node: ast.Name, scope: Scope) -> Any:
        value = scope.get(node.id)
        if value is UNBOUND:
            raise LanguageError(f"undefined name {node.id!r}", line=node.lineno)
        return value

    def _ex_JoinedStr(self, node: ast.JoinedStr, scope: Scope) -> str:
        return "".join(str(self._eval(part, scope)) for part in node.values)

    def _ex_FormattedValue(self, node: ast.FormattedValue, scope: Scope) -> str:
        value = self._eval(node.value, scope)
        # Conversion applies before the format spec, as in Python: the result
        # reaches the hashed IR and must match CPython's rendering.
        if (convert := CONVERSIONS.get(node.conversion)) is not None:
            value = self._render(value, convert)
        if node.format_spec is not None:
            spec = self._eval(node.format_spec, scope)
            # `f"{1:>1000000000}"`: the width sizes the output, charged before it is built.
            self._tick(format_cost(spec))
            if not isinstance(value, str):
                self._charge_deep(value)
                value = stable_form(value)
            return format(value, spec)
        return self._render(value)

    def _ex_BinOp(self, node: ast.BinOp, scope: Scope) -> Any:
        op_type = type(node.op)
        left = self._eval(node.left, scope)
        right = self._eval(node.right, scope)
        return self._apply_binop(op_type, left, right)

    def _ex_UnaryOp(self, node: ast.UnaryOp, scope: Scope) -> Any:
        return UNARYOPS[type(node.op)](self._eval(node.operand, scope))

    def _ex_BoolOp(self, node: ast.BoolOp, scope: Scope) -> Any:
        # Short-circuit: return the deciding operand's value, or the last one.
        stop_on = not isinstance(node.op, ast.And)
        result: Any = None
        for value_node in node.values:
            result = self._eval(value_node, scope)
            if bool(result) == stop_on:
                return result
        return result

    def _ex_Compare(self, node: ast.Compare, scope: Scope) -> bool:
        left = self._eval(node.left, scope)
        for op, right_node in zip(node.ops, node.comparators, strict=True):
            right = self._eval(right_node, scope)
            self._tick(compare_cost(op, left, right, self._remaining()))
            if not COMPARES[type(op)](left, right):
                return False
            left = right
        return True

    def _ex_IfExp(self, node: ast.IfExp, scope: Scope) -> Any:
        chosen = node.body if self._eval(node.test, scope) else node.orelse
        return self._eval(chosen, scope)

    def _ex_Attribute(self, node: ast.Attribute, scope: Scope) -> Any:
        obj = self._eval(node.value, scope)
        # `validate` checks only the spelling; what an attribute exposes depends
        # on the object, so the policy is re-applied here.
        if (reason := attribute_rejection(node.attr)) is not None or not _attribute_allowed(
            obj, node.attr
        ):
            raise LanguageError(
                reason
                or f"attribute {node.attr!r} of {describe_type(obj)} is not "
                "accessible from Atlas-lang",
                line=node.lineno,
                col=node.col_offset + 1,
            )
        value = getattr(obj, node.attr)
        # Defence in depth: whatever name reached it, config never holds an
        # interpreter internal or a module.
        if isinstance(value, _UNREACHABLE_TYPES):
            raise LanguageError(
                f"attribute {node.attr!r} of {describe_type(obj)} is not accessible "
                "from Atlas-lang",
                line=node.lineno,
                col=node.col_offset + 1,
            )
        return value

    def _ex_Subscript(self, node: ast.Subscript, scope: Scope) -> Any:
        container, key = self._subscript(node, scope)
        return container[key]

    def _ex_List(self, node: ast.List, scope: Scope) -> list[Any]:
        return [self._eval(e, scope) for e in node.elts]

    def _ex_Tuple(self, node: ast.Tuple, scope: Scope) -> tuple[Any, ...]:
        return tuple(self._eval(e, scope) for e in node.elts)

    def _ex_Set(self, node: ast.Set, scope: Scope) -> set[Any]:
        return self._make_set(self._eval(e, scope) for e in node.elts)

    def _make_set(self, items: Iterable[Any]) -> set[Any]:
        out: set[Any] = set()
        for item in items:
            self._charge_key(item)
            out.add(item)
        return out

    def _ex_Dict(self, node: ast.Dict, scope: Scope) -> dict[Any, Any]:
        result: dict[Any, Any] = {}
        for key_node, value_node in zip(node.keys, node.values, strict=True):
            if key_node is None:
                raise LanguageError("dict unpacking (**) is not supported", line=node.lineno)
            key = self._eval(key_node, scope)
            self._charge_key(key)
            result[key] = self._eval(value_node, scope)
        return result

    def _ex_Call(self, node: ast.Call, scope: Scope) -> Any:
        func = self._eval(node.func, scope)
        args, kwargs = self._call_args(node, scope)
        if not isinstance(func, Closure):
            func, args, kwargs = self._native_prepared(func, args, kwargs)
        try:
            return func(*args, **kwargs)
        except LanguageError as exc:
            # A native callable (`Config(...)`, `enforce(...)`, `output(...)`)
            # has no source position, so its `LanguageError` gets this call's.
            # Other errors (a `RegistryError`, a pydantic failure from a
            # resource constructor) concern the value, not this line.
            if exc.line is None:
                raise LanguageError(str(exc), line=node.lineno, col=node.col_offset + 1) from exc
            raise

    def _call_args(self, node: ast.Call, scope: Scope) -> tuple[list[Any], dict[str, Any]]:
        """Evaluate a call's arguments left to right, expanding ``*iterable``."""
        args: list[Any] = []
        for arg in node.args:
            if isinstance(arg, ast.Starred):
                self._expand_star(self._eval(arg.value, scope), args)
            else:
                args.append(self._eval(arg, scope))
        # `kw.arg is None` marks `**mapping`, which is unsupported; it raises
        # rather than being skipped, as in `_ex_Dict`.
        for kw in node.keywords:
            if kw.arg is None:
                raise LanguageError(
                    "`**` keyword unpacking is not supported; pass keywords explicitly",
                    line=node.lineno,
                )
        kwargs = {kw.arg: self._eval(kw.value, scope) for kw in node.keywords if kw.arg}
        return args, kwargs

    def _expand_star(self, value: Any, out: list[Any]) -> None:
        """Append the items of ``*value`` to ``out``, charging one step per item.

        Charged before the list is built, not by the call's cost afterwards:
        ``f(*range(10**9))`` would otherwise allocate gigabytes first. A sized
        value is charged its length up front; an unsized iterator (``map``,
        ``zip``) per chunk as it is drained.
        """
        try:
            size: int | None = len(value)
        except TypeError:
            size = None
        except OverflowError:
            # `range(10**20)`: too long to count, so certainly too long to expand.
            size = self._remaining() + 1
        if size is not None:
            self._tick(size)
            out.extend(self._iter(value))
            return
        pending = 0
        for item in self._iter(value):
            out.append(item)
            pending += 1
            if pending == _STAR_CHUNK:
                self._tick(pending)
                pending = 0
        self._tick(pending)

    def _call_native(self, func: Any, args: list[Any], kwargs: dict[str, Any]) -> Any:
        """Call native ``func`` as a call written in config does (see `_ex_Call`)."""
        func, args, kwargs = self._native_prepared(func, args, kwargs)
        return func(*args, **kwargs)

    def _native_prepared(
        self, func: Any, args: list[Any], kwargs: dict[str, Any]
    ) -> tuple[Any, list[Any], dict[str, Any]]:
        """``func`` and its arguments as a native builtin must receive them.

        Native builtins loop and allocate outside the interpreter, so they are
        metered here by input size, up front. Set arguments are sorted, since
        native iteration follows ``PYTHONHASHSEED`` order; so is each set nested
        in a model's arguments, which pydantic lowers to a list in iteration
        order. ``set.pop`` takes the first element in sorted order, ``str``
        renders through `_render`, and the callable a higher-order native calls
        back through is routed back here. A bound method of a built-in container
        or string is also charged for walking its receiver (``big.copy()``),
        unless it is one of the constant-time `_RECEIVER_FREE` methods.
        """
        self._tick(native_call_cost(args, kwargs, self._remaining(), func=func))
        receiver = getattr(func, "__self__", None)
        if (
            isinstance(receiver, _RECEIVERS)
            and getattr(func, "__name__", None) not in _RECEIVER_FREE
        ):
            self._tick(deep_cost(receiver, self._remaining()))
        if func is str:
            return self._str, args, kwargs
        if isinstance(func, type) and issubclass(func, BaseModel):
            args = [self._model_arg(a) for a in args]
            kwargs = {k: self._model_arg(v) for k, v in kwargs.items()}
            return func, args, kwargs
        if func is _SET_POP:  # `set.pop(s)`
            return self._set_pop, args, kwargs
        owner = getattr(func, "__self__", None)
        if isinstance(owner, set) and getattr(func, "__name__", None) == "pop":
            return functools.partial(self._set_pop, owner), args, kwargs
        args = [self._normalize_arg(a) for a in args]
        kwargs = {k: self._normalize_arg(v) for k, v in kwargs.items()}
        # Wrap only the argument the native calls: elsewhere `str` is a value,
        # which the native may return (`max([], default=str)`) or store.
        slot = callback_slot(func)
        if slot == 0 and args:
            args[0] = self._callback(args[0])
        elif slot == "key" and "key" in kwargs:
            kwargs["key"] = self._callback(kwargs["key"])
        return func, args, kwargs

    def _callback(self, func: Any) -> Any:
        """``func`` as a higher-order native may call it.

        A config function already runs on this interpreter, and ``None`` is the
        natives' own "no function"; anything else is native, so its calls are
        routed back through `_call_native` (`map(str, xs)` renders each element
        as `str(x)` does, `map(list, [s])` sorts the set first).
        """
        if func is None or isinstance(func, Closure):
            return func
        return _NativeCallback(self, func)

    def _normalize_arg(self, value: Any) -> Any:
        """A top-level set or frozenset argument as a deterministically ordered list.

        Keeps native builtins (``list``, ``str.join``, ``sum``) from exposing
        hash-seed order."""
        if isinstance(value, frozenset | set):
            return self._sorted_set(value)
        return value

    def _model_arg(self, value: Any) -> Any:
        """``value`` with every set in it, at any depth, a list in iteration order.

        For a model's arguments: pydantic converts a set into the list a
        ``list[str]`` field declares in hash order, which would reach the IR. A
        field declaring a set still receives one. Returns ``value`` unchanged
        when it holds no set; fuel is already charged for the call's arguments.
        """
        return self._sets_as_lists(value) if holds_set(value) else value

    def _sets_as_lists(self, value: Any) -> Any:
        if isinstance(value, frozenset | set):
            return [self._sets_as_lists(item) for item in self._sorted_set(value)]
        if isinstance(value, dict):
            # Keys stay: a frozenset key cannot become a list.
            return {k: self._sets_as_lists(v) for k, v in value.items()}
        if isinstance(value, list | tuple):
            items = [self._sets_as_lists(item) for item in value]
            return items if isinstance(value, list) else tuple(items)
        return value

    def _ex_Lambda(self, node: ast.Lambda, scope: Scope) -> Closure:
        return self._make_closure(node.args, node.body, scope, "<lambda>")

    def _ex_ListComp(self, node: ast.ListComp, scope: Scope) -> list[Any]:
        return self._comp_elements(node, scope)

    # Generator expressions are materialised eagerly as lists.
    _ex_GeneratorExp = _ex_ListComp

    def _ex_SetComp(self, node: ast.SetComp, scope: Scope) -> set[Any]:
        return self._make_set(self._comp_elements(node, scope))

    def _ex_DictComp(self, node: ast.DictComp, scope: Scope) -> dict[Any, Any]:
        out: dict[Any, Any] = {}

        def emit(s: Scope) -> None:
            key = self._eval(node.key, s)
            self._charge_key(key)
            out[key] = self._eval(node.value, s)

        self._run_comp(node.generators, 0, scope, emit)
        return out

    def _comp_elements(
        self, node: ast.ListComp | ast.SetComp | ast.GeneratorExp, scope: Scope
    ) -> list[Any]:
        """Run a comprehension's generators and collect ``elt`` for each match."""
        out: list[Any] = []
        self._run_comp(node.generators, 0, scope, lambda s: out.append(self._eval(node.elt, s)))
        return out
