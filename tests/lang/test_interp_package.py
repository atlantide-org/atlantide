"""Invariants the interpreter's module layout must keep.

The evaluator is a base class plus statement/expression mixins, found by name
at dispatch. These pin what that layout could silently break: which objects
config can never read an attribute of, the name-based dispatch, the reprs the
determinism guard relies on, and the fuel prices for operators.
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Callable
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

import atlantide.lang.interp as interp
from atlantide.core import FuelExhaustedError, LanguageError, is_successful
from atlantide.core._describe import ADDRESS
from atlantide.lang import evaluate_source
from atlantide.lang.builtins import build_globals
from atlantide.lang.interp import MAX_INT_BITS, Closure, Interpreter, Scope
from atlantide.lang.interp.costs import _int_result_bits, binop_cost
from atlantide.lang.interp.evaluator import _Evaluator
from atlantide.lang.interp.expressions import ExpressionsMixin, _attribute_allowed
from atlantide.lang.interp.stability import require_stable_repr
from atlantide.lang.interp.statements import StatementsMixin


def test_package_exports() -> None:
    assert set(interp.__all__) == {
        "DEFAULT_FUEL",
        "MAX_INT_BITS",
        "Closure",
        "Interpreter",
        "Scope",
        "bind_rejection",
    }


# -- the attribute policy covers every interpreter object ----------------------


def _closure() -> Closure:
    return Closure([], [], ast.Constant(1), Scope(), Interpreter(), "f")


@pytest.mark.parametrize(
    "obj",
    [
        _closure(),
        Scope(),
        Interpreter(),
        _Evaluator(),
        StatementsMixin(),
        ExpressionsMixin(),
    ],
    ids=["closure", "scope", "interpreter", "evaluator", "statements", "expressions"],
)
@pytest.mark.parametrize("name", ["interp", "scope", "run", "fuel", "surface", "vars", "get"])
def test_no_attribute_of_an_interpreter_object_is_readable(obj: object, name: str) -> None:
    assert not _attribute_allowed(obj, name)


def test_every_handler_host_is_an_evaluator() -> None:
    """`_attribute_allowed` refuses `_Evaluator`; that only covers the
    interpreter while every class carrying handlers derives from it."""
    for cls in (Interpreter, StatementsMixin, ExpressionsMixin):
        assert issubclass(cls, _Evaluator)


@pytest.mark.parametrize(
    "source",
    [
        "def f():\n    return 1\nx = f.interp\n",
        "def f():\n    return 1\nx = f.scope\n",
        "f = lambda: 1\nx = f.params\n",
    ],
)
def test_config_cannot_reach_the_evaluator_through_a_function(source: str) -> None:
    result = evaluate_source(source)
    assert not is_successful(result)
    assert "not accessible from Atlas-lang" in str(result.failure())


# -- dispatch ------------------------------------------------------------------


def test_dispatch_charges_before_looking_up_the_handler() -> None:
    module = ast.parse("while x:\n    pass\n")  # never validated: no `_st_While`
    with pytest.raises(FuelExhaustedError):
        Interpreter(fuel=0).run(module, Scope(init=build_globals({})))


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("while x:\n    pass\n", "cannot execute While"),
        ("x = (y := 1)\n", "cannot evaluate NamedExpr"),
    ],
)
def test_a_node_without_a_handler_is_refused_by_name(source: str, message: str) -> None:
    with pytest.raises(LanguageError, match=message):
        Interpreter().run(ast.parse(source), Scope(init=build_globals({})))


def test_handlers_are_found_on_the_interpreter() -> None:
    names = {n for n in dir(Interpreter) if n.startswith(("_st_", "_ex_"))}
    assert {"_st_ClassDef", "_st_ImportFrom", "_st_With", "_ex_Call", "_ex_Attribute"} <= names
    assert Interpreter._ex_GeneratorExp is Interpreter._ex_ListComp


# -- reprs the determinism guard depends on --------------------------------------


@pytest.mark.parametrize(
    ("obj", "prefix"),
    [
        (_closure(), "<function f at 0x"),
        (Closure([], [], ast.Constant(1), Scope(), Interpreter()), "<lambda at 0x"),
        (Scope(), "<atlantide.lang.interp.scope.Scope object at 0x"),
    ],
    ids=["def", "lambda", "scope"],
)
def test_reprs_of_config_reachable_objects_embed_an_address(obj: object, prefix: str) -> None:
    """The address is what `require_stable_repr` refuses; neither repr may lose it."""
    text = repr(obj)
    assert text.startswith(prefix)
    assert ADDRESS.search(text)
    with pytest.raises(LanguageError, match="memory layout"):
        require_stable_repr([obj])


def test_a_closure_repr_does_not_print_the_interpreter() -> None:
    text = repr(_closure())
    assert "Scope" not in text
    assert "Interpreter" not in text
    assert "ast." not in text


@pytest.mark.parametrize(
    "source",
    [
        "def f():\n    return 1\nx = str(f)\n",
        "x = f'{[lambda: 1]}'\n",
    ],
)
def test_rendering_a_function_is_refused(source: str) -> None:
    result = evaluate_source(source)
    assert not is_successful(result)
    assert "memory layout" in str(result.failure())


# -- operator fuel prices --------------------------------------------------------

_INT_OPS: dict[type[ast.operator], Callable[[int, int], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.LShift: operator.lshift,
    ast.RShift: operator.rshift,
    ast.BitOr: operator.or_,
    ast.BitAnd: operator.and_,
    ast.BitXor: operator.xor,
    ast.Pow: operator.pow,
}


@given(
    op_type=st.sampled_from(sorted(_INT_OPS, key=lambda t: t.__name__)),
    left=st.integers(min_value=-(2**80), max_value=2**80),
    right=st.integers(min_value=-(2**80), max_value=2**80),
)
def test_int_result_bits_bounds_the_real_result(
    op_type: type[ast.operator], left: int, right: int
) -> None:
    if op_type in (ast.LShift, ast.RShift, ast.Pow):
        right = abs(right) % 200
    if op_type in (ast.FloorDiv, ast.Mod) and right == 0:
        right = 1
    result = _INT_OPS[op_type](left, right)
    if isinstance(result, int):
        assert _int_result_bits(op_type, left, right) >= result.bit_length()


@pytest.mark.parametrize(
    ("op_type", "left", "right", "cost"),
    [
        (ast.Mult, "ab", 3, 6),
        (ast.Mult, 3, "ab", 6),
        (ast.Mult, [1, 2], -1, 0),
        (ast.Mult, 2.0, "ab", 0),
        (ast.Mod, "%s-%s", (1, 2), 5),
        (ast.Mod, [1], 2, 0),
        (ast.Add, "a" * 2000, "b" * 100, 2100 // 16),
        (ast.Add, "a", "b", 0),
        (ast.BitOr, {1: 1} | {}, {2: 2}, 0),
        (ast.Div, "a" * 5000, 2, 0),
        (ast.Mult, 2**200, 2**200, 402 // 64),
        (ast.Pow, 1, 10**9, 0),
    ],
)
def test_binop_cost(op_type: type[ast.operator], left: Any, right: Any, cost: int) -> None:
    assert binop_cost(op_type, left, right) == cost


def test_binop_cost_refuses_an_oversized_integer() -> None:
    with pytest.raises(FuelExhaustedError, match=f"exceed {MAX_INT_BITS} bits"):
        binop_cost(ast.LShift, 1, MAX_INT_BITS)
