"""The Atlas-lang interpreter: the evaluator core plus every node handler."""

from __future__ import annotations

import ast
from dataclasses import dataclass

from atlantide.lang.interp.expressions import ExpressionsMixin
from atlantide.lang.interp.scope import Scope, Signal
from atlantide.lang.interp.statements import StatementsMixin


@dataclass
class Interpreter(StatementsMixin, ExpressionsMixin):
    """Runs a validated module.

    Its fields (``fuel``, ``surface``) are declared on `_Evaluator`; the mixins
    and this class add none, so ``__init__`` and ``__repr__`` list exactly those.
    """

    def run(self, module: ast.Module, scope: Scope) -> None:
        try:
            for stmt in module.body:
                self._exec(stmt, scope)
        except Signal as signal:
            # `validate` rejects a module-level `break`/`continue`/`return`.
            raise signal.stray() from None
