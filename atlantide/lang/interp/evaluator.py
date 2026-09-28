"""The evaluator core every node handler builds on: fuel, dispatch, binding.

:class:`_Evaluator` holds the interpreter's only state (the fuel budget and
what has been spent) and routes each node to a handler by name. The handlers
themselves live in ``statements`` and ``expressions`` as mixins over this
class; :class:`~atlantide.lang.interp.Interpreter` combines them. Every
evaluator object is an ``_Evaluator``, and the attribute policy
(``expressions._attribute_allowed``) refuses config every attribute of one.
"""

from __future__ import annotations

import ast
import itertools
import operator
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from atlantide.core.errors import FuelExhaustedError, LanguageError
from atlantide.lang.interp.costs import (
    DEFAULT_FUEL,
    NESTED,
    binop_cost,
    deep_cost,
    slice_cost,
)
from atlantide.lang.interp.operators import BINOPS
from atlantide.lang.interp.scope import Closure, Scope, _Break, _Continue, _Return
from atlantide.lang.interp.stability import stable_form, stable_repr
from atlantide.lang.validate import DEFAULT_SURFACE, LanguageSurface


@dataclass
class _Evaluator:
    fuel: int = DEFAULT_FUEL
    #: Which modules config may import; see :class:`LanguageSurface`.
    surface: LanguageSurface = DEFAULT_SURFACE
    _spent: int = field(default=0, init=False)

    def _tick(self, cost: int = 1) -> None:
        """Charge ``cost`` evaluation steps against the fuel budget.

        ``cost`` is one per interpreter node, or an up-front size estimate for
        native work (a builtin over a sized argument, a large multiply or power),
        so evaluation halts even inside a native loop."""
        self._spent += cost
        if self._spent > self.fuel:
            raise FuelExhaustedError(
                f"evaluation exceeded fuel budget ({self.fuel} steps); raise it with "
                "--fuel or [lang] fuel in atlantide.toml"
            )

    def _remaining(self) -> int:
        return max(self.fuel - self._spent, 0)

    def _charge_deep(self, value: Any) -> None:
        """Charge for a native walk over a container (hash, repr, serialize).

        A scalar renders or hashes in time proportional to memory that already
        exists, so it costs nothing beyond the node that produced it."""
        if isinstance(value, NESTED):
            self._tick(deep_cost(value, self._remaining()))

    def _charge_key(self, key: Any) -> None:
        """Hashing a tuple/frozenset key walks it natively, and its hash is not cached."""
        if isinstance(key, tuple | frozenset):
            self._charge_deep(key)

    def _iter(self, value: Any) -> Iterator[Any]:
        if isinstance(value, frozenset | set):
            return iter(self._sorted_set(value))
        return iter(value)

    def _sorted_set(self, value: set[Any] | frozenset[Any]) -> list[Any]:
        """A set's elements in an order independent of hash seed and memory layout.

        Sorted by `stable_repr`, the order in which the set renders, so ``str(s)``
        and ``for x in s`` agree. Two distinct elements that render identically
        would fall back to hash order, so they raise.
        """
        self._charge_deep(value)
        keyed = sorted(((stable_repr(v), v) for v in value), key=operator.itemgetter(0))
        for (a, _), (b, _) in itertools.pairwise(keyed):
            if a == b:
                raise LanguageError(
                    f"set holds distinct elements that render alike ({a}); "
                    "their order would not be deterministic"
                )
        return [v for _, v in keyed]

    def _set_pop(self, *args: Any, **kwargs: Any) -> Any:
        """``s.pop()`` (or ``set.pop(s)``), removing the first element in iteration order.

        Native ``set.pop`` removes the first element in hash order, which depends
        on ``PYTHONHASHSEED``."""
        if len(args) != 1 or kwargs or not isinstance(args[0], set):
            raise TypeError("set.pop() takes no arguments: call it as s.pop() on a set")
        target: set[Any] = args[0]
        if not target:
            raise KeyError("pop from an empty set")
        first = self._sorted_set(target)[0]
        target.remove(first)
        return first

    def _render(self, value: Any, convert: Callable[[Any], str] = str) -> str:
        """``str``/``repr``/``ascii`` of ``value``, bounded and deterministic."""
        self._charge_deep(value)
        return convert(stable_form(value))

    def _str(self, *args: Any, **kwargs: Any) -> str:
        """The ``str`` builtin as config calls it: rendering goes through `_render`.

        Never handed to config or to a native: a native reaches ``str`` through
        `expressions._NativeCallback`, which calls back here."""
        if len(args) == 1 and not kwargs:
            return self._render(args[0])
        return str(*args, **kwargs)

    def _apply_binop(self, op_type: type[ast.operator], left: Any, right: Any) -> Any:
        self._tick(binop_cost(op_type, left, right))
        if op_type is ast.Mod and isinstance(left, str | bytes):
            # printf-style `"%r" % obj` renders obj as str()/repr() do.
            self._charge_deep(right)
            right = stable_form(right)
        return BINOPS[op_type](left, right)

    def _exec(self, node: ast.stmt, scope: Scope) -> None:
        self._dispatch("_st_", "execute", node, scope)

    def _eval(self, node: ast.expr, scope: Scope) -> Any:
        return self._dispatch("_ex_", "evaluate", node, scope)

    def _dispatch(self, prefix: str, verb: str, node: ast.stmt | ast.expr, scope: Scope) -> Any:
        """Route one node to its ``_st_``/``_ex_`` handler.

        Handlers are looked up by method name, so supporting a node type means
        adding one method; a node type without a handler raises.
        """
        self._tick()
        method = getattr(self, prefix + type(node).__name__, None)
        if method is None:
            raise LanguageError(f"cannot {verb} {type(node).__name__}", line=node.lineno)
        return method(node, scope)

    def _make_closure(
        self, args: ast.arguments, body: list[ast.stmt] | ast.expr, scope: Scope, name: str
    ) -> Closure:
        # `validate` rejects these with a line number; this guards unvalidated trees.
        if args.vararg or args.kwarg or args.posonlyargs or args.kwonlyargs:
            raise LanguageError("only simple positional/default params are supported")
        params = [a.arg for a in args.args]
        defaults = [self._eval(d, scope) for d in args.defaults]
        return Closure(params, defaults, body, scope, self, name)

    def _invoke(self, closure: Closure, args: list[Any], kwargs: dict[str, Any]) -> Any:
        call_scope = self._bind_params(closure, args, kwargs)
        if isinstance(closure.body, list):
            try:
                for stmt in closure.body:
                    self._exec(stmt, call_scope)
            except _Return as ret:
                return ret.value
            except (_Break, _Continue) as signal:
                # `validate` rejects `break`/`continue` outside a loop; one that
                # runs anyway must not affect the caller's loop.
                raise signal.stray() from None
            return None
        return self._eval(closure.body, call_scope)

    @staticmethod
    def _bind_params(closure: Closure, args: list[Any], kwargs: dict[str, Any]) -> Scope:
        """A call's scope with every parameter bound; raises the first binding error.

        Errors are checked in a fixed order (missing argument, surplus positionals,
        unknown keywords), so a bad call always reports the same one.
        """
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
        if len(args) > len(params):
            # The binding loop reads one arg per parameter and ignores any surplus.
            raise LanguageError(
                f"{closure.name}() takes {len(params)} argument(s) but {len(args)} were given"
            )
        if kwargs:
            raise LanguageError(f"{closure.name}() got unexpected keyword(s) {list(kwargs)}")
        return call_scope

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
            for sub, item in zip(target.elts, self._unpack(target, value), strict=True):
                self._bind(sub, item, scope)
        elif isinstance(target, ast.Starred):
            self._bind(target.value, value, scope)  # already the starred list
        elif isinstance(target, ast.Subscript):
            container, key = self._subscript(target, scope)
            container[key] = value
        else:
            raise LanguageError(f"cannot assign to {type(target).__name__}", line=target.lineno)

    def _unpack(self, target: ast.Tuple | ast.List, value: Any) -> list[Any]:
        """``value`` split into one item per element of ``target``, as Python unpacks.

        Without a starred element at most ``len(target.elts) + 1`` items are
        read, so ``a, b = range(10**8)`` fails at once rather than materializing
        the range. A starred element collects the rest into a list, one step
        per item.
        """
        count = len(target.elts)
        stars = [i for i, elt in enumerate(target.elts) if isinstance(elt, ast.Starred)]
        it = self._iter(value)
        if not stars:
            items = list(itertools.islice(it, count + 1))
            if len(items) > count:
                raise LanguageError(
                    f"too many values to unpack (expected {count})", line=target.lineno
                )
            if len(items) < count:
                raise LanguageError(
                    f"not enough values to unpack (expected {count}, got {len(items)})",
                    line=target.lineno,
                )
            return items
        if len(stars) > 1:
            # `ast.parse` accepts this; CPython rejects it only at compile time.
            raise LanguageError(
                "syntax error: multiple starred expressions in assignment", line=target.lineno
            )
        items = []
        for item in it:
            self._tick()
            items.append(item)
        if len(items) < count - 1:
            raise LanguageError(
                f"not enough values to unpack (expected at least {count - 1}, got {len(items)})",
                line=target.lineno,
            )
        star, after = stars[0], count - 1 - stars[0]
        rest = items[star : len(items) - after]
        return [*items[:star], rest, *items[len(items) - after :]]

    def _eval_load_target(self, target: ast.expr, scope: Scope) -> Any:
        # For AugAssign: the current value of a Name/Subscript target.
        if isinstance(target, ast.Name):
            return scope.lookup(target.id)
        if isinstance(target, ast.Subscript):
            container, key = self._subscript(target, scope)
            return container[key]
        raise LanguageError(f"cannot read {type(target).__name__}", line=target.lineno)

    def _subscript(self, node: ast.Subscript, scope: Scope) -> tuple[Any, Any]:
        """Evaluate a subscript target into its ``(container, key)`` pair."""
        container, key = self._eval(node.value, scope), self._eval_slice(node.slice, scope)
        self._charge_key(key)
        self._tick(slice_cost(container, key))
        return container, key

    def _eval_slice(self, node: ast.expr, scope: Scope) -> Any:
        if isinstance(node, ast.Slice):
            lower = self._eval(node.lower, scope) if node.lower else None
            upper = self._eval(node.upper, scope) if node.upper else None
            step = self._eval(node.step, scope) if node.step else None
            return slice(lower, upper, step)
        return self._eval(node, scope)
