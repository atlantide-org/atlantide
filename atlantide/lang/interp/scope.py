"""Lexical scopes, user functions, and the signals that unwind control flow.

``Closure`` is reachable from config (a ``def`` evaluates to one), so its
representation is part of the determinism contract. Like a native function's
repr and ``Scope``'s default one, it embeds an address, so `stability` refuses
to render it. Error messages describe either through `atlantide.core._describe`,
not ``repr``.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol, override

from atlantide.core.errors import LanguageError

# Sentinel for "name not found", distinct from every config value including None.
UNBOUND = object()


class Scope:
    """A lexical scope with a parent chain (module -> function/comprehension)."""

    __slots__ = ("parent", "vars")

    def __init__(self, parent: Scope | None = None, init: dict[str, Any] | None = None) -> None:
        self.vars: dict[str, Any] = dict(init) if init else {}
        self.parent = parent

    def get(self, name: str) -> Any:
        """Resolve ``name`` up the parent chain, or return ``UNBOUND``."""
        scope: Scope | None = self
        while scope is not None:
            if name in scope.vars:
                return scope.vars[name]
            scope = scope.parent
        return UNBOUND

    def lookup(self, name: str) -> Any:
        value = self.get(name)
        if value is UNBOUND:
            raise LanguageError(f"undefined name {name!r}")
        return value

    def assign(self, name: str, value: Any) -> None:
        self.vars[name] = value


class Signal(Exception):
    """Unwinds control flow to the loop or call that handles it.

    ``validate`` rejects each one outside its construct, as Python's compiler
    does. Should one escape anyway, `stray` turns it into a syntax error so the
    class name never reaches config as error text.
    """

    #: CPython's compile-time message for the statement outside its construct.
    outside: ClassVar[str]

    def __init__(self, node: ast.stmt) -> None:
        self.node = node

    def stray(self) -> LanguageError:
        return LanguageError(
            f"syntax error: {self.outside}", line=self.node.lineno, col=self.node.col_offset + 1
        )


class _Return(Signal):
    outside = "'return' outside function"

    def __init__(self, node: ast.stmt, value: Any) -> None:
        super().__init__(node)
        self.value = value


class _Break(Signal):
    outside = "'break' outside loop"


class _Continue(Signal):
    outside = "'continue' not properly in loop"


class Invoker(Protocol):
    """What a :class:`Closure` calls back into: the evaluator that defined it."""

    def _invoke(self, closure: Closure, args: list[Any], kwargs: dict[str, Any]) -> Any: ...


@dataclass(repr=False)
class Closure:
    """A user-defined ``def``/``lambda``, callable from Python (map/sorted/...)."""

    params: list[str]
    defaults: list[Any]
    body: list[ast.stmt] | ast.expr
    scope: Scope
    interp: Invoker
    name: str = "<lambda>"

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.interp._invoke(self, list(args), kwargs)

    @property
    def __name__(self) -> str:
        """The name it was defined with, as a native function reports it, so
        `atlantide.core._describe` names it the same way."""
        return self.name

    @override
    def __repr__(self) -> str:
        # Matches a native function's repr, address included, so `stability`
        # refuses to render it. The dataclass default would put the AST, scope
        # and interpreter into any message that shows the function (a native
        # `ValueError`, a pydantic error).
        label = "<lambda" if self.name == "<lambda>" else f"<function {self.name}"
        return f"{label} at {id(self):#x}>"
