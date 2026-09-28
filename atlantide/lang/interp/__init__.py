"""Atlas-lang tree-walking interpreter.

Evaluates the validated AST directly, without CPython's ``exec``. Determinism
follows from the evaluator: no clock, random, env, network or file API is in the
namespace, sets iterate in sorted order, and a fuel counter bounds every
evaluation. See ``lang/README.md`` for the module map.
"""

from __future__ import annotations

from atlantide.lang.interp.binding import bind_rejection
from atlantide.lang.interp.costs import DEFAULT_FUEL, MAX_INT_BITS
from atlantide.lang.interp.interpreter import Interpreter
from atlantide.lang.interp.scope import Closure, Scope

__all__ = [
    "DEFAULT_FUEL",
    "MAX_INT_BITS",
    "Closure",
    "Interpreter",
    "Scope",
    "bind_rejection",
]
