"""An ``EnvSchema`` default through the interpreter: same verdict as ``var()``, on its line.

The interpreter builds a schema class with its defaults kept out of the class
namespace (``__atlas_defaults__``), a different path from ordinary Python — so
parity with ``var()`` is pinned here as well as in ``tests/core``, from source
an author would write.
"""

from __future__ import annotations

import pytest
from returns.result import Failure

from atlantide.core import LanguageError
from atlantide.core.config import SUPPORTED_FIELD_TYPE_NAMES
from atlantide.lang import evaluate_source

#: Defaults as written in a config file.
_VALUES = [
    "'x'",
    "0",
    "7",
    "1.5",
    "True",
    "False",
    "[1]",
    "{'a': 1}",
    "None",
    "(1,)",
    "lambda: 1",
    "str",
]

_CASES = [
    (type_name, value) for type_name in sorted(SUPPORTED_FIELD_TYPE_NAMES) for value in _VALUES
]


def _outcome(source: str) -> LanguageError | None:
    result = evaluate_source(source)
    if not isinstance(result, Failure):
        return None
    error = result.failure()
    assert isinstance(error, LanguageError), error
    return error


def _message(error: LanguageError | None) -> str | None:
    """The message without its ``(line N, col M)`` suffix."""
    if error is None:
        return None
    return str(error).removesuffix(f" (line {error.line}, col {error.col})")


def _by_var(type_name: str, value: str) -> str:
    return (
        "from atlantide.core import Config, var\n"
        f"schema = {{'x': var({type_name}, default={value})}}\n"
        "config = Config(schema=schema, envs={'dev': {}})\n"
    )


def _by_class(annotation: str, value: str) -> str:
    return (
        "from atlantide.core import Config, EnvSchema\n"
        "class AppEnv(EnvSchema):\n"
        "    ok: int = 1\n"
        f"    x: {annotation} = {value}\n"
        "config = Config(AppEnv, envs={'dev': {}})\n"
    )


@pytest.mark.parametrize("nullable", [False, True], ids=["plain", "nullable"])
@pytest.mark.parametrize(("type_name", "value"), _CASES)
def test_a_schema_default_is_refused_exactly_when_var_refuses_it(
    type_name: str, value: str, nullable: bool
) -> None:
    annotation = f"{type_name} | None" if nullable else type_name
    expected = _message(_outcome(_by_var(type_name, value)))
    actual = _outcome(_by_class(annotation, value))

    if expected is None:
        assert actual is None
    else:
        assert _message(actual) == expected.replace(f"var({type_name})", "AppEnv.x", 1)
        assert actual is not None
        assert (actual.line, actual.col) == (
            4,
            len("    x: ") + len(annotation) + len(" = ") + 1,
        )  # the default


def test_the_reported_example_now_fails_at_the_field() -> None:
    source = (
        "from atlantide.core import EnvSchema\n"
        "class AppEnv(EnvSchema):\n"
        "    size: int = 'x'\n"
        "    retries: int = (lambda: 1)\n"
    )
    error = _outcome(source)
    assert error is not None
    assert str(error) == "AppEnv.size default 'x' is a str (line 3, col 17)"

    error = _outcome(source.replace("= 'x'", "= 1"))
    assert error is not None
    assert str(error) == "AppEnv.retries default <lambda> is a function (line 4, col 21)"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
