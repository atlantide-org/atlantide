"""Functions a native builtin calls on config's behalf (``map(f, xs)``, ``key=``).

The interpreter routes such a call back through itself so it behaves as the
same call written in config does: ``str`` renders through the address check,
a set argument is sorted, and the work costs fuel. What routes it is an
interpreter object, so it is only ever put where the native *calls* it; in any
other position ``str`` is a value, and a native that returns a value hands
config the real builtin — never an object whose text shows interpreter state.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest

from atlantide.core import FuelExhaustedError, LanguageError, is_successful
from atlantide.lang import evaluate_source
from atlantide.lang.builtins import build_globals
from atlantide.lang.interp import Interpreter, Scope
from atlantide.lang.interp.expressions import _attribute_allowed, _NativeCallback
from atlantide.lang.interp.stability import require_stable_repr
from atlantide.lang.validate import validate_source
from tests.lang.test_interp import Widget, _base_env


def _run(source: str, **kw: Any) -> dict[str, Any]:
    """Evaluate ``source`` and return its module-level variables."""
    scope = Scope(init=build_globals())
    Interpreter(**kw).run(validate_source(source).unwrap(), scope)
    return scope.vars


def _label(source: str, **kw: Any) -> str:
    reg = evaluate_source(source, extra_globals={"Widget": Widget}, **kw).unwrap()
    label = reg.get("default:test.Widget:w").unwrap().label
    assert isinstance(label, str)
    return label


# -- `str` as a value is the builtin itself ------------------------------------

VALUE_POSITIONS = [
    ("max_default", "x = max([], default=str)"),
    ("min_default", "x = min([], default=str)"),
    ("max_default_with_key", "x = max([], key=len, default=str)"),
    ("max_of_callables", "x = max([str], key=bool)"),
    ("max_key_str_of_callables", "x = max([str], key=str)"),
    ("sorted_element", "x = sorted([str])[0]"),
    ("dict_get_default", "x = {}.get('k', str)"),
    ("list_element", "x = [str][0]"),
    ("map_identity", "x = list(map(lambda f: f, [str]))[0]"),
    ("filter_element", "x = list(filter(None, [str]))[0]"),
]


@pytest.mark.parametrize(("name", "source"), VALUE_POSITIONS, ids=[n for n, _ in VALUE_POSITIONS])
def test_str_passed_as_a_value_comes_back_as_the_builtin(name: str, source: str) -> None:
    assert _run(source)["x"] is str


def test_max_default_str_renders_as_python_does() -> None:
    """`max([], default=str)` is `str`, and renders as `<class 'str'>` like in Python."""
    source = "f = max([], default=str)\nWidget('w', size=1, label=f'{f}|{f!r}|{f(5)}')"
    assert _label(source) == "<class 'str'>|<class 'str'>|5"


def test_max_default_str_renders_the_same_on_every_run() -> None:
    """Guards against the rendered text embedding the interpreter's fuel counters,
    which would move the field (and the IR hash) with the budget and steps spent."""
    source = (
        "pad = [i for i in range(n)]\nf = max([], default=str)\nWidget('w', size=1, label=f'{f}')"
    )
    labels = {
        _label(source.replace("range(n)", f"range({n})"), fuel=fuel)
        for n in (0, 50)
        for fuel in (10_000, 1_000_000)
    }
    assert labels == {"<class 'str'>"}
    (label,) = labels
    for internal in ("Interpreter", "_Evaluator", "fuel", "bound method", "callback"):
        assert internal not in label


# -- `str` as a callback still renders through the check ------------------------

CALLBACKS = [
    ("map", "x = list(map(str, [1, 'a', None]))", ["1", "a", "None"]),
    ("filter", "x = list(filter(str, ['', 'a']))", ["a"]),
    ("sorted_key", "x = sorted([10, 9, 100], key=str)", [10, 100, 9]),
    ("min_key", "x = min([10, 9], key=str)", 10),
    ("max_key", "x = max(10, 9, key=str)", 9),
    ("list_sort_key", "x = [10, 9, 100]\nx.sort(key=str)", [10, 100, 9]),
    ("native_key", "x = sorted(['ccc', 'a', 'bb'], key=len)", ["a", "bb", "ccc"]),
    ("closure_key", "x = sorted([3, 1, 2], key=lambda n: -n)", [3, 2, 1]),
    ("no_key", "x = sorted([2, 1], key=None)", [1, 2]),
]


@pytest.mark.parametrize(
    ("name", "source", "expected"), CALLBACKS, ids=[n for n, _, _ in CALLBACKS]
)
def test_callbacks_behave_as_in_python(name: str, source: str, expected: object) -> None:
    assert _run(source)["x"] == expected


def test_a_non_callable_callback_fails_as_in_python() -> None:
    result = evaluate_source("x = list(map(5, [1]))")
    assert not is_successful(result)
    assert "'int' object is not callable" in str(result.failure())


# -- a native called by a native is called as config would call it --------------


@pytest.mark.parametrize(
    "source",
    [
        "def f():\n    return 1\nx = list(map(list, map(map, [str], [[f]])))",
        "def f():\n    return 1\nx = sorted([[f]], key=lambda xs: list(map(str, xs)))",
        "inner = list(map(map, [str], [[merge]]))[0]\nx = list(inner)",
    ],
    ids=["map_of_map_str", "closure_calling_map_str", "inner_map_consumed_later"],
)
def test_str_inside_a_nested_native_refuses_an_address(source: str) -> None:
    """`map(map, [str], [[f]])` calls `map(str, [f])` natively; unrouted, that
    `str` would be the unchecked builtin and render `<function f at 0x...>`."""
    result = evaluate_source(source)
    assert not is_successful(result)
    assert "memory layout" in str(result.failure())


def test_a_set_given_to_a_native_callback_is_sorted() -> None:
    assert _run("x = list(map(list, [{'c', 'a', 'b'}]))")["x"] == [["a", "b", "c"]]


def test_a_set_given_to_a_native_callback_is_hash_seed_independent() -> None:
    prog = (
        "from atlantide.lang.builtins import build_globals\n"
        "from atlantide.lang.interp import Interpreter, Scope\n"
        "from atlantide.lang.validate import validate_source\n"
        "src = \"x = '-'.join(list(map(list, [{'b','a','c','d','e'}]))[0])\\n\"\n"
        "scope = Scope(init=build_globals())\n"
        "Interpreter().run(validate_source(src).unwrap(), scope)\n"
        "print(scope.vars['x'])\n"
    )
    outputs = set()
    for seed in ("0", "1", "42", "1337"):
        proc = subprocess.run(
            [sys.executable, "-c", prog],
            capture_output=True,
            text=True,
            env={**_base_env(), "PYTHONHASHSEED": seed},
        )
        assert proc.returncode == 0, proc.stderr
        outputs.add(proc.stdout.strip())
    assert outputs == {"a-b-c-d-e"}


@pytest.mark.parametrize(
    "source",
    [
        "x = list(map(sum, map(range, [10 ** 6])))",
        "x = list(map(len, map(list, map(range, [10 ** 6]))))",
        "x = max(map(range, [10 ** 6]), key=sum)",
    ],
    ids=["map_sum_range", "map_list_range", "max_key_sum"],
)
def test_a_native_callback_costs_fuel(source: str) -> None:
    """Without routing, `sum(range(N))` called by `map` would run natively for free:
    the outer call is charged for its arguments, and a `map` object is small."""
    result = evaluate_source(source, fuel=10_000)
    assert not is_successful(result)
    assert isinstance(result.failure(), FuelExhaustedError)


# -- the routing object never reaches config, and is inert if it did ------------


def test_the_routing_object_cannot_be_rendered() -> None:
    callback = _NativeCallback(Interpreter(), str)
    with pytest.raises(LanguageError, match="memory layout"):
        require_stable_repr(callback)
    with pytest.raises(LanguageError, match="memory layout"):
        require_stable_repr([callback])


@pytest.mark.parametrize("name", ["upper", "join", "evaluator", "func", "_func", "_evaluator"])
def test_no_attribute_of_the_routing_object_is_readable(name: str) -> None:
    assert not _attribute_allowed(_NativeCallback(Interpreter(), str), name)
