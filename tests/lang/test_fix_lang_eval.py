"""Regression tests: unpacking, comparison, output-size and constant-time fuel
prices, single evaluation of an augmented-assignment target, and validator
rejections (deep nesting, decorators, unsupported parameter kinds)."""

from __future__ import annotations

import ast
from typing import Any

import pytest

from atlantide.core import FuelExhaustedError, LanguageError, is_successful
from atlantide.lang import evaluate_source, validate_source
from atlantide.lang.builtins import build_globals
from atlantide.lang.interp import Interpreter, Scope
from atlantide.lang.interp.costs import (
    CONSTANT_TIME_METHODS,
    binop_cost,
    compare_cost,
    format_cost,
    native_call_cost,
    slice_cost,
)


def _run(source: str, fuel: int = 5_000_000) -> Scope:
    scope = Scope(init=build_globals({}))
    Interpreter(fuel=fuel).run(ast.parse(source), scope)
    return scope


# -- item 1: unpacking reads at most len(targets) + 1 items ---------------------


def test_unpacking_a_huge_iterable_fails_without_materializing() -> None:
    with pytest.raises(LanguageError, match=r"too many values to unpack \(expected 2\)"):
        _run("a, b = range(10**8)")


def test_unpacking_too_few_values() -> None:
    with pytest.raises(LanguageError, match=r"not enough values to unpack \(expected 3, got 2\)"):
        _run("a, b, c = [1, 2]")


def test_unpacking_exact_and_nested() -> None:
    scope = _run("(a, b), c = [(1, 2), 3]")
    assert (scope.lookup("a"), scope.lookup("b"), scope.lookup("c")) == (1, 2, 3)


def test_starred_unpacking() -> None:
    scope = _run("a, *b, c = range(5)\nd, *e = 'x'")
    assert scope.lookup("a") == 0
    assert scope.lookup("b") == [1, 2, 3]
    assert scope.lookup("c") == 4
    assert (scope.lookup("d"), scope.lookup("e")) == ("x", [])


def test_starred_unpacking_too_few_values() -> None:
    with pytest.raises(LanguageError, match=r"expected at least 3, got 2"):
        _run("a, *b, c, d = [1, 2]")


def test_starred_unpacking_is_charged() -> None:
    with pytest.raises(FuelExhaustedError):
        _run("a, *b = range(10**8)", fuel=100_000)


# -- item 2: comparison walks only as far as the smaller side ------------------


def test_comparing_a_huge_shared_structure_with_a_small_one_is_cheap() -> None:
    big: list[Any] = [1]
    for _ in range(60):
        big = [big, big]  # 2**60 nodes to a native walk
    assert compare_cost(ast.Eq(), big, [1], 10**15) < 100
    assert compare_cost(ast.Eq(), [1], big, 10**15) < 100


def test_comparing_two_huge_structures_exhausts() -> None:
    big: list[Any] = [1]
    for _ in range(60):
        big = [big, big]
    assert compare_cost(ast.Eq(), big, big, 10_000) > 10_000


# -- item 3: output-size blowups -----------------------------------------------


@pytest.mark.parametrize(
    ("func", "args"),
    [
        ("a".ljust, [10**9]),
        ("a".rjust, [10**9]),
        ("a".center, [10**9]),
        ("1".zfill, [10**9]),
        (str.ljust, ["a", 10**9]),
        ((1).to_bytes, [10**9, "big"]),
        (("x" * 1000).join, [["a"] * 100_000]),
        (("ab" * 10_000).replace, ["a", "ab" * 10_000]),
    ],
)
def test_output_size_is_charged(func: Any, args: list[Any]) -> None:
    assert native_call_cost(args, {}, 5_000_000, func=func) > 5_000_000


def test_ordinary_padding_and_join_stay_cheap() -> None:
    assert native_call_cost([10], {}, 5_000_000, func="a".ljust) < 5
    assert native_call_cost([["a", "b"]], {}, 5_000_000, func="-".join) < 10


def test_sum_with_a_list_start_is_refused() -> None:
    with pytest.raises(LanguageError, match="quadratic"):
        native_call_cost([[[1], [2]], []], {}, 5_000_000, func=sum)
    assert native_call_cost([[1, 2]], {}, 5_000_000, func=sum) == 5  # the walk only


@pytest.mark.parametrize("spec", [">1000000000", ".100000000f", "0>1000000000"])
def test_format_spec_width_is_charged(spec: str) -> None:
    assert format_cost(spec) > 5_000_000


def test_small_format_spec_is_free() -> None:
    assert format_cost(">10") == 0


@pytest.mark.parametrize(
    "source",
    ["x = '%*d' % (10**9, 1)", "x = '%1000000000d' % 1", "x = '%.100000000f' % 1.0"],
)
def test_printf_width_is_charged(source: str) -> None:
    result = evaluate_source(source)
    assert isinstance(result.failure(), FuelExhaustedError)


def test_printf_literal_digits_are_not_widths() -> None:
    assert binop_cost(ast.Mod, "build 20240101: %s", ("x",)) == len("build 20240101: %s")


# -- item 4: operations over existing memory -----------------------------------


@pytest.mark.parametrize(
    "check",
    ["'b' in s", "s == t", "s < t", "s[1:]", "5000 in d.values()"],
)
def test_linear_ops_over_existing_memory_are_charged(check: str) -> None:
    source = (
        "s = 'a' * 100000\nt = 'a' * 100000\nd = {i: i for i in range(20000)}\n"
        f"for _ in range(2000):\n    x = {check}"
    )
    result = evaluate_source(source, fuel=700_000)
    assert isinstance(result.failure(), FuelExhaustedError)


def test_compare_prices_text_and_views() -> None:
    s = "a" * 6400
    assert compare_cost(ast.In(), "b", s, 10**9) == 100
    assert compare_cost(ast.Eq(), s, s, 10**9) == 100
    assert compare_cost(ast.In(), 1, {i: i for i in range(100)}.values(), 10**9) >= 100
    assert compare_cost(ast.In(), 1, {i: i for i in range(100)}.keys(), 10**9) < 5


def test_slice_cost() -> None:
    assert slice_cost("a" * 6400, slice(None)) == 100
    assert slice_cost(list(range(1600)), slice(0, None, 1)) == 100
    assert slice_cost(range(10**9), slice(None)) == 0
    assert slice_cost({"a": 1}, "a") == 0


# -- item 5: constant-time builtins cost a flat step -----------------------------


def test_len_does_not_walk_its_argument() -> None:
    rows = [{"a": i, "b": [i] * 10} for i in range(2000)]
    assert native_call_cost([rows], {}, 5_000_000, func=len) == 1
    assert native_call_cost([rows], {}, 5_000_000, func=bool) == 1


def test_constant_time_methods_do_not_walk_their_arguments() -> None:
    row = {"a": list(range(1000))}
    assert native_call_cost([row], {}, 5_000_000, func=[].append) == 1
    assert native_call_cost(["a"], {}, 5_000_000, func={}.get) == 1
    assert native_call_cost([(1,) * 1000], {}, 5_000_000, func={}.get) > 1000  # hashes the key
    expected = {"append", "get", "setdefault", "pop", "keys", "values", "items"}
    assert expected == CONSTANT_TIME_METHODS


# -- item 6: an augmented-assignment target is evaluated once -------------------


def test_aug_assign_evaluates_container_and_key_once() -> None:
    source = (
        "calls = []\nd = {0: 1}\n"
        "def k():\n    calls.append(1)\n    return 0\n"
        "def c():\n    calls.append(2)\n    return d\n"
        "c()[k()] += 5"
    )
    scope = _run(source)
    assert scope.lookup("calls") == [2, 1]
    assert scope.lookup("d") == {0: 6}


# -- items 7-9: validator ------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "x = " + "+".join(["1"] * 3000),
        "x = " + "-" * 100_000 + "1",
        "x = " + "not " * 100_000 + "1",
    ],
)
def test_deep_nesting_is_a_failure(source: str) -> None:
    for result in (validate_source(source), evaluate_source(source)):
        assert not is_successful(result)
        assert "nested too deeply" in str(result.failure())


@pytest.mark.parametrize(
    ("source", "line"),
    [
        ("x = 1\n@thing\ndef f():\n    pass", 3),
        ("x = 1\ndef f(*args):\n    pass", 2),
        ("def f(**kw):\n    pass", 1),
        ("def f(*, a):\n    pass", 1),
        ("def f(a, /, b):\n    pass", 1),
        ("x = 1\ny = lambda *a: 0", 2),
        ("y = lambda *, a: a", 1),
    ],
)
def test_unsupported_function_forms_are_rejected_with_a_line(source: str, line: int) -> None:
    result = validate_source(source)
    assert not is_successful(result)
    failure = result.failure()
    assert isinstance(failure, LanguageError)
    assert failure.line == line


def test_simple_params_still_validate() -> None:
    assert is_successful(validate_source("def f(a, b=1):\n    return a\ng = lambda x, y=2: x"))
