"""Regression tests: interpreter internals reached through public attribute
names, and native work (star-arg expansion, receiver walks) that went uncharged.

Every sandbox probe here is harmless: it reads a flag or compares with ``None``
and asserts that the read itself is refused.
"""

from __future__ import annotations

import ast
import types
from typing import Any

import pytest

from atlantide.core import FuelExhaustedError, LanguageError, is_successful
from atlantide.lang import evaluate_source, validate_source
from atlantide.lang.builtins import build_globals
from atlantide.lang.interp import Interpreter, Scope
from atlantide.lang.validate import attribute_rejection

_REGION = 'from atlantide.core import region\ncm = region("us-east-1")\n'


def _run_unvalidated(source: str, **extra: Any) -> None:
    """Evaluate ``source`` skipping `validate`, so only the runtime policy applies."""
    namespace = build_globals({})
    namespace.update(extra)
    Interpreter().run(ast.parse(source), Scope(init=namespace))


# -- item 1: generator / frame / code internals -------------------------------


@pytest.mark.parametrize(
    "expr",
    ["cm.gen.gi_running", "cm.gen is not None", "cm.func is not None", "cm.kwds is not None"],
)
def test_generator_context_manager_internals_are_refused(expr: str) -> None:
    result = evaluate_source(f"{_REGION}x = {expr}")
    assert not is_successful(result)
    assert isinstance(result.failure(), LanguageError)


@pytest.mark.parametrize(
    "name",
    [
        "gi_frame",
        "gi_code",
        "gi_yieldfrom",
        "cr_frame",
        "cr_await",
        "ag_frame",
        "f_globals",
        "f_builtins",
        "f_back",
        "tb_frame",
        "tb_next",
        "co_code",
        "co_consts",
    ],
)
def test_interpreter_internal_attribute_names_are_rejected_statically(name: str) -> None:
    result = validate_source(f"x = y.{name}")
    assert not is_successful(result)
    assert isinstance(result.failure(), LanguageError)


@pytest.mark.parametrize(
    ("expr", "held"),
    [
        ("g.close is not None", (i for i in ())),
        ("g.send is not None", (i for i in ())),
        ("c.replace is not None", (lambda: 0).__code__),
    ],
    ids=["generator.close", "generator.send", "code.replace"],
)
def test_internals_are_refused_by_type_whatever_the_name(expr: str, held: Any) -> None:
    """Names no static rule lists: only the type check in `_attribute_allowed` stops them."""
    name = expr.split(".")[1].split()[0]
    assert attribute_rejection(name) is None
    with pytest.raises(LanguageError):
        _run_unvalidated(f"x = {expr}", g=held, c=held)


@pytest.mark.parametrize(
    "held",
    [types, (lambda: 0).__code__, (i for i in ())],
    ids=["module", "code", "generator"],
)
def test_attribute_results_that_are_internals_are_refused(held: Any) -> None:
    holder = types.SimpleNamespace(value=held)
    with pytest.raises(LanguageError):
        _run_unvalidated("x = holder.value is not None", holder=holder)


def test_ordinary_attributes_still_read() -> None:
    holder = types.SimpleNamespace(value=3)
    _run_unvalidated("x = holder.value + 1", holder=holder)


# -- item 2: star-arg expansion is charged before it allocates ----------------


def test_huge_star_arg_exhausts_fuel_without_materializing() -> None:
    result = evaluate_source("x = max(*range(10**12))")
    assert not is_successful(result)
    assert isinstance(result.failure(), FuelExhaustedError)


def test_star_arg_into_a_config_function_is_charged() -> None:
    source = "def f(a):\n    return a\nx = f(*range(200000))"
    result = evaluate_source(source, fuel=100_000)
    assert isinstance(result.failure(), FuelExhaustedError)


def test_unsized_star_arg_is_charged_while_expanding() -> None:
    source = "xs = list(range(50000))\nm = map(abs, xs)\nx = max(*m)"
    result = evaluate_source(source, fuel=120_000)
    assert isinstance(result.failure(), FuelExhaustedError)


def test_small_star_arg_still_works() -> None:
    assert is_successful(evaluate_source("x = max(*[1, 2, 3])"))


# -- item 3: a bound method's receiver is charged ------------------------------


def test_copying_a_large_receiver_is_charged() -> None:
    source = "big = list(range(1000))\nxs = [big.copy() for _ in range(300)]"
    result = evaluate_source(source, fuel=100_000)
    assert isinstance(result.failure(), FuelExhaustedError)


def test_constant_time_methods_do_not_charge_the_receiver() -> None:
    source = "d = {i: i for i in range(1000)}\nxs = [d.get(1) for _ in range(300)]"
    assert is_successful(evaluate_source(source, fuel=100_000))


def test_growing_a_set_in_a_loop_does_not_charge_the_receiver() -> None:
    source = "s = set()\nfor i in range(3000):\n    s.add(i)"
    assert is_successful(evaluate_source(source, fuel=100_000))


# -- costs wiring: output-size charges reach config ---------------------------


def test_padding_to_a_huge_width_exhausts_fuel() -> None:
    result = evaluate_source("x = 'a'.ljust(10**9)")
    assert isinstance(result.failure(), FuelExhaustedError)


def test_a_huge_format_spec_width_exhausts_fuel() -> None:
    result = evaluate_source('x = f"{1:>1000000000}"')
    assert isinstance(result.failure(), FuelExhaustedError)


def test_len_in_a_loop_over_small_dicts_stays_cheap() -> None:
    source = "rows = [{'a': i} for i in range(2000)]\nn = 0\nfor r in rows:\n    n = n + len(rows)"
    assert is_successful(evaluate_source(source))
