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
    SecretRef,
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


def test_an_input_reaches_the_config_as_its_value() -> None:
    src = "Widget('w', size=atlantide.input('n'), label='x')"
    reg = _eval(src, inputs={"n": 7}).unwrap()
    assert reg.get("default:test.Widget:w").unwrap().size == 7


def test_a_secret_is_a_handle_not_a_value() -> None:
    """`atlantide.secret()` used to read from `inputs` and return the plaintext.

    That put the value straight into a resource field, and from there into the
    hashed IR, the `.atlas` artifact and the state store — a secret written down
    in three places by the one function whose purpose is to keep it out of them.
    It now returns a `SecretRef`: the *name* travels, the value is resolved
    in-memory at apply.
    """
    src = "Widget('w', size=1, label=atlantide.secret('tok'))"
    reg = _eval(src, inputs={"tok": "hunter2"}).unwrap()

    label = reg.get("default:test.Widget:w").unwrap().label
    assert isinstance(label, SecretRef)
    assert label.name == "tok"
    assert "hunter2" not in repr(label), "the value never enters the config graph"


def test_a_secret_ignores_a_same_named_input_entirely() -> None:
    """Not even as a fallback: an input is a visible, recorded value, and letting
    one satisfy a `secret()` would reintroduce the leak by another route."""
    src = "Widget('w', size=1, label=atlantide.secret('tok'))"
    reg = _eval(src, inputs={"tok": "hunter2"}).unwrap()
    assert reg.inputs == {}, "a secret handle consumes no input"


def test_only_the_inputs_the_config_read_are_recorded() -> None:
    """An input passed but never read must not look like part of the plan's
    identity — otherwise a stray variable set for another tool changes what the
    run appears to be."""
    src = "Widget('w', size=atlantide.input('n'), label='x')"
    reg = _eval(src, inputs={"n": 7, "unused": "whatever"}).unwrap()
    assert reg.inputs == {"n": 7}


def test_a_taken_default_is_recorded_too() -> None:
    """It shaped the config just as much as a passed value did."""
    src = "Widget('w', size=atlantide.input('n', 3), label='x')"
    assert _eval(src).unwrap().inputs == {"n": 3}


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


class _Recorder:
    """A context manager recording what its ``__exit__`` was handed."""

    def __init__(self, *, suppress: bool = False) -> None:
        self.suppress = suppress
        self.exits: list[tuple[object, object]] = []

    def __enter__(self) -> _Recorder:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        self.exits.append((exc_type, exc))
        return self.suppress


class _FailingEnter:
    def __enter__(self) -> None:
        raise RuntimeError("enter failed")

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


def test_with_passes_the_exception_to_exit() -> None:
    cm = _Recorder()
    result = evaluate_source("with cm:\n    x = 1 // 0\n", extra_globals={"cm": cm})
    assert not is_successful(result)
    [(exc_type, exc)] = cm.exits
    assert exc_type is ZeroDivisionError
    assert isinstance(exc, ZeroDivisionError)


def test_with_honours_a_suppressing_exit() -> None:
    cm = _Recorder(suppress=True)
    src = "with cm:\n    x = 1 // 0\nWidget('w', size=1)\n"
    reg = evaluate_source(src, extra_globals={"cm": cm, "Widget": Widget}).unwrap()
    assert len(reg) == 1  # execution continued past the with block
    [(exc_type, _)] = cm.exits
    assert exc_type is ZeroDivisionError


def test_with_unwinds_entered_managers_when_a_later_enter_fails() -> None:
    first = _Recorder()
    result = evaluate_source(
        "with a, b:\n    pass\n", extra_globals={"a": first, "b": _FailingEnter()}
    )
    assert not is_successful(result)
    [(exc_type, exc)] = first.exits  # the already-entered manager was exited
    assert exc_type is RuntimeError
    assert "enter failed" in str(exc)


def test_with_exits_cleanly_on_break() -> None:
    cm = _Recorder()
    src = "for i in range(3):\n    with cm:\n        break\n"
    evaluate_source(src, extra_globals={"cm": cm}).unwrap()
    assert cm.exits == [(None, None)]  # control flow is a non-exceptional exit


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


def test_fstring_conversion_composes_with_format_spec() -> None:
    """`!r` applies before the spec, exactly as Python: f"{v!r:>6}"."""
    src = "Widget('w', size=1, label=f\"{'ab'!r:>6}\")"
    reg = _eval(src).unwrap()
    assert reg.get("default:test.Widget:w").unwrap().label == f"{'ab'!r:>6}"
