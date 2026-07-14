"""Atlas-lang interpreter: evaluation, determinism, fuel, resource collection."""

from __future__ import annotations

import os
import subprocess
import sys
from typing import ClassVar

from atlantide.core import (
    FuelExhaustedError,
    LanguageError,
    Resource,
    immutable,
    is_successful,
    mutable,
)
from atlantide.lang import evaluate_source


def _base_env() -> dict[str, str]:
    """Inherit PATH/venv so the subprocess can import atlantide."""
    keep = ("PATH", "VIRTUAL_ENV", "PYTHONPATH", "HOME")
    return {k: os.environ[k] for k in keep if k in os.environ}


class Widget(Resource):
    """Test resource injected via extra_globals (no provider package yet)."""

    class Meta:
        provider: ClassVar[str] = "test"

    size: int = immutable()
    label: str = mutable(default="")


def _eval(source: str, **kw: object) -> object:
    return evaluate_source(source, extra_globals={"Widget": Widget}, **kw)  # type: ignore[arg-type]


# -- evaluation --------------------------------------------------------------


def test_arithmetic_and_names() -> None:
    reg = _eval("Widget('w', size=2 ** 3 + 1)").unwrap()
    assert reg.get("default:test.Widget:w").unwrap().size == 9


def test_for_loop_generates_n_resources() -> None:
    src = "for i in range(5):\n    Widget(f'w{i}', size=i)"
    reg = _eval(src).unwrap()
    assert len(reg) == 5
    assert [r.logical_name for r in reg.all()] == ["w0", "w1", "w2", "w3", "w4"]


def test_comprehension_and_function() -> None:
    src = (
        "def double(n):\n"
        "    return n * 2\n"
        "sizes = [double(i) for i in range(3)]\n"
        "for idx, s in enumerate(sizes):\n"
        "    Widget(f'w{idx}', size=s)"
    )
    reg = _eval(src).unwrap()
    assert sorted(r.size for r in reg.all()) == [0, 2, 4]


def test_fstring_and_dependency_ref() -> None:
    src = "a = Widget('a', size=1)\nWidget('b', size=2, label=f'after-{a.label}')"
    reg = _eval(src).unwrap()
    b = reg.get("default:test.Widget:b").unwrap()
    # a.label is a concrete default (""), so f-string resolves eagerly here
    assert b.label == "after-"


def test_closure_passed_to_builtin() -> None:
    src = "vals = sorted([3, 1, 2], key=lambda x: -x)\nWidget('w', size=vals[0])"
    reg = _eval(src).unwrap()
    assert reg.get("default:test.Widget:w").unwrap().size == 3


def test_inputs_and_secret() -> None:
    src = "Widget('w', size=atlantide.input('n'), label=atlantide.secret('tok'))"
    reg = _eval(src, inputs={"n": 7, "tok": "hunter2"}).unwrap()
    w = reg.get("default:test.Widget:w").unwrap()
    assert w.size == 7 and w.label == "hunter2"


def test_pure_derived_builtins() -> None:
    src = "Widget('w', size=1, label=sha256_hex('x')[:8])"
    reg = _eval(src).unwrap()
    # deterministic hash prefix
    assert reg.get("default:test.Widget:w").unwrap().label == "2d711642"


# -- failure modes -----------------------------------------------------------


def test_with_stack_namespaces_resources() -> None:
    from atlantide.core import Stack

    src = (
        "for env in ['dev', 'prod']:\n"
        "    with Stack(env, region='us-east-1'):\n"
        "        Widget('w', size=1)\n"
    )
    reg = evaluate_source(src, extra_globals={"Widget": Widget, "Stack": Stack}).unwrap()
    assert {r.node_id for r in reg.all()} == {
        "dev:test.Widget:w",
        "prod:test.Widget:w",
    }


def test_undefined_nondeterministic_names() -> None:
    for expr in ("time.time()", "random()", "os.environ"):
        result = _eval(f"Widget('w', size=1, label=str({expr}))")
        assert not is_successful(result)
        assert isinstance(result.failure(), LanguageError)


def test_fuel_exhaustion() -> None:
    src = "for i in range(10_000):\n    Widget(f'w{i}', size=i)"
    result = _eval(src, fuel=200)
    assert not is_successful(result)
    assert isinstance(result.failure(), FuelExhaustedError)


def test_missing_required_input() -> None:
    result = _eval("Widget('w', size=atlantide.input('nope'))")
    assert not is_successful(result)
    assert isinstance(result.failure(), LanguageError)


def test_typed_validation_surfaces_as_failure() -> None:
    result = _eval("Widget('w', size='not-an-int')")
    assert not is_successful(result)
    assert isinstance(result.failure(), LanguageError)


# -- determinism -------------------------------------------------------------


def test_set_iteration_is_sorted() -> None:
    src = "vals = [x for x in {5, 3, 1, 4, 2}]\nWidget('w', size=vals[0], label=str(vals))"
    reg = _eval(src).unwrap()
    assert reg.get("default:test.Widget:w").unwrap().label == "[1, 2, 3, 4, 5]"


def test_set_order_stable_across_hash_seeds() -> None:
    """The interpreter's set iteration must not depend on PYTHONHASHSEED."""
    prog = (
        "from atlantide.lang import evaluate_source\n"
        "src = \"joined = '-'.join(x for x in {'b','a','c','d','e'})\\n\"\n"
        "namespace = {}\n"
        "# capture the module-level var by evaluating into a shared dict\n"
        "from atlantide.lang.builtins import build_globals\n"
        "from atlantide.lang.interp import Interpreter, Scope\n"
        "from atlantide.lang.validate import validate_source\n"
        "mod = validate_source(src).unwrap()\n"
        "scope = Scope(init=build_globals())\n"
        "Interpreter().run(mod, scope)\n"
        "print(scope.vars['joined'])\n"
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


def test_native_builtin_over_set_is_sorted() -> None:
    """Sets passed straight into a native builtin (not via a comprehension) must
    also be normalised — otherwise `'-'.join(set)` leaks PYTHONHASHSEED."""
    src = "Widget('w', size=1, label='-'.join({'b', 'a', 'c', 'd', 'e'}))"
    reg = _eval(src).unwrap()
    assert reg.get("default:test.Widget:w").unwrap().label == "a-b-c-d-e"


def test_list_of_set_is_sorted() -> None:
    src = "Widget('w', size=1, label=str(list({3, 1, 2})))"
    reg = _eval(src).unwrap()
    assert reg.get("default:test.Widget:w").unwrap().label == "[1, 2, 3]"


# -- fuel bounds native work -------------------------------------------------


def test_fuel_bounds_native_builtin() -> None:
    """A single native call over a huge iterable must hit the fuel limit rather
    than run unbounded (`sum(range(N))` is one interpreter step)."""
    result = _eval("Widget('w', size=sum(range(10_000)))", fuel=200)
    assert not is_successful(result)
    assert isinstance(result.failure(), FuelExhaustedError)


def test_fuel_bounds_string_repetition() -> None:
    result = _eval("Widget('w', size=1, label='a' * 10_000)", fuel=200)
    assert not is_successful(result)
    assert isinstance(result.failure(), FuelExhaustedError)


def test_fuel_bounds_integer_power() -> None:
    result = _eval("Widget('w', size=2 ** 10_000_000)", fuel=200)
    assert not is_successful(result)
    assert isinstance(result.failure(), FuelExhaustedError)


def test_small_native_calls_within_budget() -> None:
    """Normal-sized native work must not be starved by the metering."""
    reg = _eval("Widget('w', size=sum(range(100)))", fuel=10_000).unwrap()
    assert reg.get("default:test.Widget:w").unwrap().size == 4950


# -- runtime errors surface as Failure ---------------------------------------


def test_zero_division_returns_failure() -> None:
    result = _eval("Widget('w', size=1 // 0)")
    assert not is_successful(result)
    assert isinstance(result.failure(), LanguageError)


def test_bad_subscript_returns_failure() -> None:
    result = _eval("Widget('w', size=[][3])")
    assert not is_successful(result)
    assert isinstance(result.failure(), LanguageError)


def test_int_parse_error_returns_failure() -> None:
    result = _eval("Widget('w', size=int('not-a-number'))")
    assert not is_successful(result)
    assert isinstance(result.failure(), LanguageError)


def test_recursion_returns_failure() -> None:
    src = "def f(n):\n    return f(n + 1)\nWidget('w', size=f(0))"
    result = _eval(src)
    assert not is_successful(result)
    assert isinstance(result.failure(), LanguageError)


# -- import escape is closed -------------------------------------------------


def test_import_module_from_internal_is_rejected() -> None:
    """The documented sandbox escape: pulling a stdlib module out of an internal
    atlantide module must fail, not hand config `importlib`."""
    result = _eval("from atlantide.lang.interp import importlib\nWidget('w', size=1)")
    assert not is_successful(result)
    assert isinstance(result.failure(), LanguageError)


def test_plain_import_module_is_rejected() -> None:
    result = _eval("import atlantide\nWidget('w', size=1)")
    assert not is_successful(result)
    assert isinstance(result.failure(), LanguageError)
