"""Atlas-lang tree-walking interpreter.

Executes the validated AST on our own evaluator — never CPython's ``exec`` — so
determinism is structural:

- clock/random/env/net/file APIs are absent from the namespace;
- iteration over sets is normalised to sorted order (avoids ``PYTHONHASHSEED``
  ordering);
- a fuel counter bounds total evaluation steps.
"""

from __future__ import annotations

import ast
import importlib
import operator
import types
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from atlantide.core.errors import FuelExhaustedError, LanguageError

DEFAULT_FUEL = 1_000_000

# Sentinel for "name not found" — distinct from any user value (including None).
_UNBOUND = object()

_BINOPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod, ast.Pow: operator.pow, ast.LShift: operator.lshift,
    ast.RShift: operator.rshift, ast.BitOr: operator.or_,
    ast.BitAnd: operator.and_, ast.BitXor: operator.xor,
}

_UNARYOPS: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.UAdd: operator.pos, ast.USub: operator.neg,
    ast.Invert: operator.invert, ast.Not: operator.not_,
}

_COMPARES: dict[type[ast.cmpop], Callable[[Any, Any], Any]] = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
    ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
    ast.Is: operator.is_, ast.IsNot: operator.is_not,
    ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,
}


def _sized_len(value: Any) -> int:
    """``len(value)`` if cheaply known, else 0 (generators/maps have no len)."""
    try:
        return len(value)
    except TypeError:
        return 0


_SEQUENCE = (str, bytes, list, tuple)


def _native_call_cost(args: list[Any], kwargs: dict[str, Any]) -> int:
    """Fuel to charge for a native builtin call: one step per argument plus one
    per element of each sized argument (``sum(range(N))`` costs ~N, so a runaway
    native loop hits the fuel limit instead of running unbounded)."""
    values = [*args, *kwargs.values()]
    return len(values) + sum(_sized_len(v) for v in values)


def _binop_cost(op_type: type[ast.operator], left: Any, right: Any) -> int:
    """Fuel for a value-producing binary op whose result can dwarf its inputs:
    sequence repetition (``"a" * N``) and integer power (``2 ** N``). Bounds the
    output size so a single node can't allocate unbounded memory for one tick."""
    if op_type is ast.Mult:
        # Normalise to (sequence, count) regardless of operand order.
        seq, count = (left, right) if isinstance(left, _SEQUENCE) else (right, left)
        if isinstance(seq, _SEQUENCE) and isinstance(count, int):
            return _sized_len(seq) * max(count, 0)
    elif op_type is ast.Pow and isinstance(left, int) and isinstance(right, int) and right > 0:
        # Result has ~ right * bit_length(left) bits; charge in machine words.
        return (right * max(left.bit_length(), 1)) // 64
    return 0


class Scope:
    """A lexical scope with a parent chain (module -> function/comprehension)."""

    __slots__ = ("parent", "vars")

    def __init__(self, parent: Scope | None = None, init: dict[str, Any] | None = None) -> None:
        self.vars: dict[str, Any] = dict(init) if init else {}
        self.parent = parent

    def get(self, name: str) -> Any:
        """Resolve ``name`` up the parent chain, or return ``_UNBOUND``."""
        scope: Scope | None = self
        while scope is not None:
            if name in scope.vars:
                return scope.vars[name]
            scope = scope.parent
        return _UNBOUND

    def lookup(self, name: str) -> Any:
        value = self.get(name)
        if value is _UNBOUND:
            raise LanguageError(f"undefined name {name!r}")
        return value

    def assign(self, name: str, value: Any) -> None:
        self.vars[name] = value


class _Return(Exception):
    def __init__(self, value: Any) -> None:
        self.value = value


class _Break(Exception):
    pass


class _Continue(Exception):
    pass


@dataclass
class Closure:
    """A user-defined ``def``/``lambda``, callable from Python (map/sorted/...)."""

    params: list[str]
    defaults: list[Any]
    body: list[ast.stmt] | ast.expr
    scope: Scope
    interp: Interpreter
    name: str = "<lambda>"

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.interp._invoke(self, list(args), kwargs)


@dataclass
class Interpreter:
    fuel: int = DEFAULT_FUEL
    _spent: int = field(default=0, init=False)

    def run(self, module: ast.Module, scope: Scope) -> None:
        for stmt in module.body:
            self._exec(stmt, scope)

    def _tick(self, cost: int = 1) -> None:
        """Charge ``cost`` evaluation steps. ``cost`` is one per interpreter node,
        or a size estimate for native work we can bound up front (a builtin over a
        sized argument, a big multiply/power) so halting stays provable even when
        evaluation drops into a native loop."""
        self._spent += cost
        if self._spent > self.fuel:
            raise FuelExhaustedError(f"evaluation exceeded fuel budget ({self.fuel} steps)")

    @staticmethod
    def _iter(value: Any) -> Iterator[Any]:
        if isinstance(value, frozenset | set):
            return iter(sorted(value, key=repr))
        return iter(value)

    def _exec(self, node: ast.stmt, scope: Scope) -> None:
        self._dispatch("_st_", "execute", node, scope)

    def _dispatch(
        self, prefix: str, verb: str, node: ast.stmt | ast.expr, scope: Scope
    ) -> Any:
        self._tick()
        method = getattr(self, prefix + type(node).__name__, None)
        if method is None:
            raise LanguageError(f"cannot {verb} {type(node).__name__}", line=node.lineno)
        return method(node, scope)

    def _st_Pass(self, node: ast.Pass, scope: Scope) -> None:
        pass

    def _st_Expr(self, node: ast.Expr, scope: Scope) -> None:
        self._eval(node.value, scope)

    def _st_Assign(self, node: ast.Assign, scope: Scope) -> None:
        value = self._eval(node.value, scope)
        for target in node.targets:
            self._bind(target, value, scope)

    def _st_AnnAssign(self, node: ast.AnnAssign, scope: Scope) -> None:
        if node.value is not None:
            self._bind(node.target, self._eval(node.value, scope), scope)

    def _st_AugAssign(self, node: ast.AugAssign, scope: Scope) -> None:
        current = self._eval_load_target(node.target, scope)
        rhs = self._eval(node.value, scope)
        op = _BINOPS[type(node.op)]
        self._bind(node.target, op(current, rhs), scope)

    def _st_If(self, node: ast.If, scope: Scope) -> None:
        branch = node.body if self._eval(node.test, scope) else node.orelse
        for stmt in branch:
            self._exec(stmt, scope)

    def _st_For(self, node: ast.For, scope: Scope) -> None:
        iterable = self._eval(node.iter, scope)
        for item in self._iter(iterable):
            self._tick()
            self._bind(node.target, item, scope)
            try:
                for stmt in node.body:
                    self._exec(stmt, scope)
            except _Break:
                break
            except _Continue:
                continue
        else:
            for stmt in node.orelse:
                self._exec(stmt, scope)

    def _st_With(self, node: ast.With, scope: Scope) -> None:
        # Enter each context manager left-to-right; exit in reverse (try/finally).
        # __enter__/__exit__ are called from the interpreter, not via config
        # attribute access, so dunder access stays banned in the language.
        managers: list[Any] = []
        for item in node.items:
            manager = self._eval(item.context_expr, scope)
            entered = type(manager).__enter__(manager)
            if item.optional_vars is not None:
                self._bind(item.optional_vars, entered, scope)
            managers.append(manager)
        try:
            for stmt in node.body:
                self._exec(stmt, scope)
        finally:
            for manager in reversed(managers):
                type(manager).__exit__(manager, None, None, None)

    def _st_Break(self, node: ast.Break, scope: Scope) -> None:
        raise _Break

    def _st_Continue(self, node: ast.Continue, scope: Scope) -> None:
        raise _Continue

    def _st_Return(self, node: ast.Return, scope: Scope) -> None:
        raise _Return(self._eval(node.value, scope) if node.value is not None else None)

    def _st_FunctionDef(self, node: ast.FunctionDef, scope: Scope) -> None:
        scope.assign(node.name, self._make_closure(node.args, node.body, scope, node.name))

    def _st_Import(self, node: ast.Import, scope: Scope) -> None:
        # Binding a whole module would expose its attribute graph (and the stdlib
        # modules it imports) to config — a sandbox escape. Only `from ... import
        # <name>` of non-module objects is allowed.
        name = node.names[0].name
        raise LanguageError(
            f"`import {name}` binds a module; use "
            f"`from {name} import <name>` for a specific public symbol",
            line=node.lineno,
        )

    def _st_ImportFrom(self, node: ast.ImportFrom, scope: Scope) -> None:
        assert node.module is not None
        module = importlib.import_module(node.module)
        for alias in node.names:
            try:
                obj = getattr(module, alias.name)
            except AttributeError:
                raise LanguageError(
                    f"cannot import {alias.name!r} from {node.module!r}", line=node.lineno
                ) from None
            # Reject importing a module object (e.g. `from atlantide.lang.interp
            # import importlib`) — that would re-open the stdlib to config.
            if isinstance(obj, types.ModuleType):
                raise LanguageError(
                    f"cannot import module {alias.name!r} from {node.module!r}; "
                    "only public classes and functions may be imported",
                    line=node.lineno,
                )
            scope.assign(alias.asname or alias.name, obj)

    def _eval(self, node: ast.expr, scope: Scope) -> Any:
        return self._dispatch("_ex_", "evaluate", node, scope)

    def _ex_Constant(self, node: ast.Constant, scope: Scope) -> Any:
        return node.value

    def _ex_Name(self, node: ast.Name, scope: Scope) -> Any:
        value = scope.get(node.id)
        if value is _UNBOUND:
            raise LanguageError(f"undefined name {node.id!r}", line=node.lineno)
        return value

    def _ex_JoinedStr(self, node: ast.JoinedStr, scope: Scope) -> str:
        return "".join(str(self._eval(part, scope)) for part in node.values)

    def _ex_FormattedValue(self, node: ast.FormattedValue, scope: Scope) -> str:
        value = self._eval(node.value, scope)
        if node.format_spec is not None:
            spec = self._eval(node.format_spec, scope)
            return format(value, spec)
        conv = node.conversion
        if conv == ord("r"):  # !r
            return repr(value)
        if conv == ord("a"):  # !a
            return ascii(value)
        return str(value)

    def _ex_BinOp(self, node: ast.BinOp, scope: Scope) -> Any:
        op_type = type(node.op)
        left = self._eval(node.left, scope)
        right = self._eval(node.right, scope)
        self._tick(_binop_cost(op_type, left, right))
        return _BINOPS[op_type](left, right)

    def _ex_UnaryOp(self, node: ast.UnaryOp, scope: Scope) -> Any:
        return _UNARYOPS[type(node.op)](self._eval(node.operand, scope))

    def _ex_BoolOp(self, node: ast.BoolOp, scope: Scope) -> Any:
        # `and` stops at the first falsy operand, `or` at the first truthy one;
        # either way the deciding (or final) operand's value is returned.
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
            if not _COMPARES[type(op)](left, right):
                return False
            left = right
        return True

    def _ex_IfExp(self, node: ast.IfExp, scope: Scope) -> Any:
        chosen = node.body if self._eval(node.test, scope) else node.orelse
        return self._eval(chosen, scope)

    def _ex_Attribute(self, node: ast.Attribute, scope: Scope) -> Any:
        return getattr(self._eval(node.value, scope), node.attr)

    def _ex_Subscript(self, node: ast.Subscript, scope: Scope) -> Any:
        container, key = self._subscript(node, scope)
        return container[key]

    def _ex_List(self, node: ast.List, scope: Scope) -> list[Any]:
        return [self._eval(e, scope) for e in node.elts]

    def _ex_Tuple(self, node: ast.Tuple, scope: Scope) -> tuple[Any, ...]:
        return tuple(self._eval(e, scope) for e in node.elts)

    def _ex_Set(self, node: ast.Set, scope: Scope) -> set[Any]:
        return {self._eval(e, scope) for e in node.elts}

    def _ex_Dict(self, node: ast.Dict, scope: Scope) -> dict[Any, Any]:
        result: dict[Any, Any] = {}
        for key_node, value_node in zip(node.keys, node.values, strict=True):
            if key_node is None:
                raise LanguageError("dict unpacking (**) is not supported", line=node.lineno)
            result[self._eval(key_node, scope)] = self._eval(value_node, scope)
        return result

    def _ex_Call(self, node: ast.Call, scope: Scope) -> Any:
        func = self._eval(node.func, scope)
        args: list[Any] = []
        for arg in node.args:
            if isinstance(arg, ast.Starred):
                args.extend(self._iter(self._eval(arg.value, scope)))
            else:
                args.append(self._eval(arg, scope))
        kwargs = {kw.arg: self._eval(kw.value, scope) for kw in node.keywords if kw.arg}
        if not isinstance(func, Closure):
            # Native builtin: it will loop/allocate outside the interpreter, so
            # meter it by input size up front and normalise set arguments to a
            # sorted order (native iteration would otherwise leak PYTHONHASHSEED).
            args = [self._normalize_arg(a) for a in args]
            kwargs = {k: self._normalize_arg(v) for k, v in kwargs.items()}
            self._tick(_native_call_cost(args, kwargs))
        return func(*args, **kwargs)

    def _normalize_arg(self, value: Any) -> Any:
        """Coerce a top-level set/frozenset argument to a deterministically
        ordered list so native builtins (``list``, ``str.join``, ``str``) don't
        expose hash-seed ordering."""
        if isinstance(value, frozenset | set):
            return list(self._iter(value))
        return value

    def _ex_Lambda(self, node: ast.Lambda, scope: Scope) -> Closure:
        return self._make_closure(node.args, node.body, scope, "<lambda>")

    def _ex_ListComp(self, node: ast.ListComp, scope: Scope) -> list[Any]:
        return self._comp_elements(node, scope)

    # Generator expressions are materialised eagerly as lists.
    _ex_GeneratorExp = _ex_ListComp

    def _ex_SetComp(self, node: ast.SetComp, scope: Scope) -> set[Any]:
        return set(self._comp_elements(node, scope))

    def _ex_DictComp(self, node: ast.DictComp, scope: Scope) -> dict[Any, Any]:
        out: dict[Any, Any] = {}

        def emit(s: Scope) -> None:
            out[self._eval(node.key, s)] = self._eval(node.value, s)

        self._run_comp(node.generators, 0, scope, emit)
        return out

    def _comp_elements(
        self, node: ast.ListComp | ast.SetComp | ast.GeneratorExp, scope: Scope
    ) -> list[Any]:
        """Run a comprehension's generators and collect ``elt`` for each match."""
        out: list[Any] = []
        self._run_comp(node.generators, 0, scope, lambda s: out.append(self._eval(node.elt, s)))
        return out

    def _make_closure(
        self, args: ast.arguments, body: list[ast.stmt] | ast.expr, scope: Scope, name: str
    ) -> Closure:
        if args.vararg or args.kwarg or args.posonlyargs or args.kwonlyargs:
            raise LanguageError("only simple positional/default params are supported")
        params = [a.arg for a in args.args]
        defaults = [self._eval(d, scope) for d in args.defaults]
        return Closure(params, defaults, body, scope, self, name)

    def _invoke(self, closure: Closure, args: list[Any], kwargs: dict[str, Any]) -> Any:
        call_scope = Scope(parent=closure.scope)
        params = closure.params
        n_required = len(params) - len(closure.defaults)
        for i, param in enumerate(params):
            if i < len(args):
                call_scope.assign(param, args[i])
            elif param in kwargs:
                call_scope.assign(param, kwargs.pop(param))
            elif i >= n_required:
                call_scope.assign(param, closure.defaults[i - n_required])
            else:
                raise LanguageError(f"{closure.name}() missing argument {param!r}")
        if kwargs:
            raise LanguageError(f"{closure.name}() got unexpected keyword(s) {list(kwargs)}")
        if isinstance(closure.body, list):
            try:
                for stmt in closure.body:
                    self._exec(stmt, call_scope)
            except _Return as ret:
                return ret.value
            return None
        return self._eval(closure.body, call_scope)

    def _run_comp(
        self,
        generators: list[ast.comprehension],
        index: int,
        scope: Scope,
        emit: Callable[[Scope], None],
    ) -> None:
        gen = generators[index]
        for item in self._iter(self._eval(gen.iter, scope)):
            self._tick()
            inner = Scope(parent=scope)
            self._bind(gen.target, item, inner)
            if all(self._eval(cond, inner) for cond in gen.ifs):
                if index + 1 < len(generators):
                    self._run_comp(generators, index + 1, inner, emit)
                else:
                    emit(inner)

    def _bind(self, target: ast.expr, value: Any, scope: Scope) -> None:
        if isinstance(target, ast.Name):
            scope.assign(target.id, value)
        elif isinstance(target, ast.Tuple | ast.List):
            items = list(self._iter(value))
            if len(items) != len(target.elts):
                raise LanguageError("unpacking count mismatch", line=target.lineno)
            for sub, item in zip(target.elts, items, strict=True):
                self._bind(sub, item, scope)
        elif isinstance(target, ast.Subscript):
            container, key = self._subscript(target, scope)
            container[key] = value
        else:
            raise LanguageError(
                f"cannot assign to {type(target).__name__}", line=target.lineno
            )

    def _eval_load_target(self, target: ast.expr, scope: Scope) -> Any:
        # For AugAssign: read the current value of a Name/Subscript target.
        if isinstance(target, ast.Name):
            return scope.lookup(target.id)
        if isinstance(target, ast.Subscript):
            container, key = self._subscript(target, scope)
            return container[key]
        raise LanguageError(f"cannot read {type(target).__name__}", line=target.lineno)

    def _subscript(self, node: ast.Subscript, scope: Scope) -> tuple[Any, Any]:
        """Evaluate a subscript target into its ``(container, key)`` pair."""
        return self._eval(node.value, scope), self._eval_slice(node.slice, scope)

    def _eval_slice(self, node: ast.expr, scope: Scope) -> Any:
        if isinstance(node, ast.Slice):
            lower = self._eval(node.lower, scope) if node.lower else None
            upper = self._eval(node.upper, scope) if node.upper else None
            step = self._eval(node.step, scope) if node.step else None
            return slice(lower, upper, step)
        return self._eval(node, scope)
