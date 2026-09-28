"""``break``/``continue``/``return`` outside the construct they belong to.

``ast.parse`` accepts them anywhere; CPython rejects them only when compiling,
which config never is. The validator reports them as CPython's compiler does
(same message, line and column), and the interpreter refuses one that runs
anyway rather than letting its internal signal escape as error text or unwind
a loop the *caller* is in.
"""

from __future__ import annotations

import ast
from typing import Any

import pytest

from atlantide.core import LanguageError, is_successful
from atlantide.lang import evaluate_source, validate_source
from atlantide.lang.builtins import build_globals
from atlantide.lang.interp import Interpreter, Scope

MISPLACED = [
    ("module_break", "break"),
    ("module_continue", "continue"),
    ("module_return", "return"),
    ("module_return_value", "x = 1\nreturn x"),
    ("function_break", "def f():\n    break"),
    ("function_continue", "def f():\n    if True:\n        continue"),
    ("def_in_loop_break", "for i in range(3):\n    def f():\n        break"),
    ("def_in_loop_continue", "for i in range(3):\n    def f():\n        continue"),
    ("for_else_break", "for i in range(3):\n    pass\nelse:\n    break"),
    (
        "for_else_continue_in_def",
        "def f():\n    for i in []:\n        pass\n    else:\n        continue",
    ),
    ("with_break", "from atlantide.core import Config\nwith Config():\n    break"),
    ("return_in_module_loop", "for i in range(3):\n    return i"),
    ("return_after_def", "def f():\n    return 1\nreturn f()"),
]


def _compile_error(source: str) -> SyntaxError:
    with pytest.raises(SyntaxError) as info:
        compile(source, "<config>", "exec")
    return info.value


@pytest.mark.parametrize(("name", "source"), MISPLACED, ids=[n for n, _ in MISPLACED])
def test_rejected_statically_as_python_does(name: str, source: str) -> None:
    expected = _compile_error(source)
    result = validate_source(source)
    assert not is_successful(result)
    error = result.failure()
    assert str(error).startswith(f"syntax error: {expected.msg} ("), str(error)
    assert (error.line, error.col) == (expected.lineno, expected.offset)
    assert "_Break" not in str(error)


@pytest.mark.parametrize(("name", "source"), MISPLACED, ids=[n for n, _ in MISPLACED])
def test_evaluation_reports_the_same_error(name: str, source: str) -> None:
    result = evaluate_source(source)
    assert not is_successful(result)
    assert str(result.failure()).startswith("syntax error: ")


VALID = [
    ("break", "x = 0\nfor i in range(10):\n    if i == 3:\n        break\n    x = i", 2),
    ("continue", "x = 0\nfor i in range(5):\n    if i % 2:\n        continue\n    x += i", 6),
    ("return", "def f(n):\n    return n + 1\nx = f(1)", 2),
    ("bare_return", "def f():\n    return\nx = f()", None),
    (
        "return_in_loop",
        "def f():\n    for i in range(9):\n        if i == 4:\n            return i\nx = f()",
        4,
    ),
    (
        "break_in_def_in_loop",
        "x = []\nfor i in range(2):\n    def f():\n        for j in range(9):\n"
        "            if j == 2:\n                break\n        return j\n    x.append(f())",
        [2, 2],
    ),
    (
        "inner_else_break_in_outer_loop",
        "x = 0\nfor i in range(5):\n    for j in []:\n        pass\n    else:\n"
        "        x = i\n        break",
        0,
    ),
    ("for_else_runs", "x = 0\nfor i in range(3):\n    pass\nelse:\n    x = 9", 9),
    (
        "break_in_function_loop_skips_else",
        "def f():\n    for i in range(3):\n        break\n    else:\n        return 'else'\n"
        "    return 'broke'\nx = f()",
        "broke",
    ),
]


@pytest.mark.parametrize(("name", "source", "expected"), VALID, ids=[n for n, _, _ in VALID])
def test_valid_control_flow_still_runs(name: str, source: str, expected: object) -> None:
    compile(source, "<config>", "exec")  # valid Python, so it must be valid config
    assert _run(ast.parse(source))["x"] == expected


# -- defence in depth: an unvalidated module --------------------------------------


def _run(module: ast.Module) -> dict[str, Any]:
    scope = Scope(init=build_globals())
    Interpreter().run(module, scope)
    return scope.vars


@pytest.mark.parametrize(
    ("source", "message", "line", "col"),
    [
        ("x = 1\nbreak", "'break' outside loop", 2, 1),
        ("continue", "'continue' not properly in loop", 1, 1),
        ("x = 1\n\nreturn x", "'return' outside function", 3, 1),
    ],
)
def test_interpreter_refuses_a_stray_signal(source: str, message: str, line: int, col: int) -> None:
    with pytest.raises(LanguageError) as info:
        _run(ast.parse(source))
    assert str(info.value) == f"syntax error: {message} (line {line}, col {col})"


def test_a_break_in_a_called_function_does_not_break_the_callers_loop() -> None:
    """Unchecked, the signal would unwind out of `f()` into the `for` around the
    call, which would stop after one iteration as though config wrote `break`."""
    source = "n = 0\nfor i in range(3):\n    def f():\n        break\n    n += 1\n    f()"
    with pytest.raises(
        LanguageError, match=r"^syntax error: 'break' outside loop \(line 4, col 9\)$"
    ):
        _run(ast.parse(source))


def test_a_continue_in_a_native_callback_is_refused() -> None:
    source = "def f(x):\n    continue\nfor i in range(3):\n    y = list(map(f, [1]))"
    with pytest.raises(LanguageError, match="'continue' not properly in loop"):
        _run(ast.parse(source))
