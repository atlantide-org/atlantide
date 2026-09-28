"""Error messages show config values without memory addresses or interpreter internals.

A message is read by a person and diffed across runs, so a value in it must be
stable and in the author's terms: plain data by ``repr`` (unchanged text), a
function as ``<lambda>``/``<function f>``, any other object by its type name.
The renderer is `atlantide.core._describe`; these pin it and every message path
that shows a config value.
"""

from __future__ import annotations

import sys
from typing import Any, ClassVar

import pytest
from hypothesis import given
from hypothesis import strategies as st

from atlantide.core import Resource, immutable, mutable
from atlantide.core._describe import (
    describe_type,
    describe_value,
    is_plain_data,
    scrub_addresses,
)
from atlantide.lang import evaluate_source
from atlantide.lang.builtins import to_json

HEADER = "from atlantide.core import Config, EnvSchema, Stack, var\n"


class Widget(Resource):
    """Test resource injected via extra_globals (no provider package needed)."""

    class Meta:
        provider: ClassVar[str] = "test"

    size: int = immutable()
    label: str = mutable(default="")


def _error(source: str) -> str:
    result = evaluate_source(HEADER + source, extra_globals={"Widget": Widget})
    return str(result.failure())


def _assert_clean(message: str) -> None:
    for leak in ("0x", "Closure", "Scope", "Interpreter", "ast."):
        assert leak not in message, message


# -- the reported case -----------------------------------------------------------


def test_a_function_default_is_named_not_dumped() -> None:
    message = _error("var(int, default=lambda: 1)\n")
    assert message == "var(int) default <lambda> is a function (line 2, col 1)"
    _assert_clean(message)


def test_a_function_default_reads_the_same_on_every_run() -> None:
    source = "def f():\n    return 1\nvar(int, default=f)\n"
    assert _error(source) == _error(source)
    assert "var(int) default <function f> is a function" in _error(source)


def test_a_data_default_keeps_its_repr() -> None:
    assert _error("var(int, default='x')\n") == "var(int) default 'x' is a str (line 2, col 1)"
    assert "var(int) default [1, 'a'] is a list" in _error("var(int, default=[1, 'a'])\n")


# -- every path that shows a config value ------------------------------------------

_ENVS = "Config(schema={'size': var(int)}, envs={'dev': {'size': 1}})"


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("var(lambda: 1)\n", "got <lambda> —"),
        ("var(int, default=[lambda: 1])\n", "default [<lambda>] is a list"),
        (
            "Config(schema={'size': var(int)}, envs={'dev': {'size': lambda: 1}})\n",
            "variable 'size' expects int, got function <lambda>",
        ),
        (
            "Config(schema={'size': lambda: 1}, envs={'dev': {}})\n",
            "schema entry 'size' must be a var(...), got function",
        ),
        (
            "Config(schema={'size': var(int)}, envs={'dev': lambda: 1})\n",
            "must be a mapping of variable to value, got function",
        ),
        (
            "Config(schema={'size': var(int)}, envs={'dev': {atlantide.to_json: 1}})\n",
            "unknown variable <function to_json> —",
        ),
        (f"{_ENVS}.env(atlantide.to_json)\n", "unknown environment <function to_json> —"),
        (
            "c = Config(schema={'region': var(list)}, envs={'dev': {'region': [lambda: 1]}})\n"
            "for e in c.envs():\n"
            "    with Stack(e.name, config=e):\n"
            "        pass\n",
            "'region' must be str, got list [<lambda>]",
        ),
        ("x = f'{(lambda: 1)}'\n", "cannot render a function as text"),
        ("f = lambda: 1\nx = f.scope\n", "attribute 'scope' of function is not accessible"),
        ("[1].index(lambda: 1)\n", "evaluation error: ValueError: <lambda> is not in list"),
        ("{'a': 1}[atlantide.to_json]\n", "evaluation error: KeyError: <function to_json>"),
        (
            "with Stack('s', region='eu-west-1'):\n    Widget('w', size=lambda: 1)\n",
            "size\n  Input should be a valid integer [type=int_type, "
            "input_value=<lambda>, input_type=function]",
        ),
        (
            "with Stack('s', region='eu-west-1'):\n    Widget('w', size=1, label=atlantide)\n",
            "input_value=<ConfigAPI object>, input_type=ConfigAPI",
        ),
    ],
    ids=[
        "var-type",
        "var-default-container",
        "env-value",
        "schema-entry",
        "env-not-mapping",
        "unknown-variable",
        "unknown-environment",
        "well-known-key",
        "render",
        "attribute",
        "native-value-error",
        "native-key-error",
        "resource-field-function",
        "resource-field-object",
    ],
)
def test_a_non_data_value_is_described(source: str, expected: str) -> None:
    message = _error(source)
    assert expected in message
    _assert_clean(message)
    assert _error(source) == message


def test_a_data_resource_field_error_keeps_pydantic_text() -> None:
    message = _error("with Stack('s', region='eu-west-1'):\n    Widget('w', size='big')\n")
    assert "input_value='big', input_type=str" in message
    assert "For further information visit" in message


def test_an_environment_value_of_the_wrong_data_type_keeps_its_text() -> None:
    message = _error("Config(schema={'size': var(int)}, envs={'dev': {'size': 'x'}})\n")
    assert "environment 'dev': variable 'size' expects int, got str 'x'" in message


# -- the renderer ------------------------------------------------------------------

_DATA = st.recursive(
    st.none()
    | st.booleans()
    | st.integers()
    | st.floats(allow_nan=False)
    | st.text(max_size=8)
    | st.binary(max_size=4),
    lambda inner: (
        st.lists(inner, max_size=3)
        | st.tuples(inner)
        | st.tuples(inner, inner)
        | st.dictionaries(st.text(max_size=4), inner, max_size=3)
        | st.frozensets(st.integers(), max_size=3)
    ),
    max_leaves=12,
)


@given(_DATA)
def test_plain_data_renders_as_its_repr(value: Any) -> None:
    assert is_plain_data(value)
    text = repr(value)
    if len(text) <= 200:
        assert describe_value(value) == text


@given(_DATA)
def test_data_beside_a_function_still_renders_as_its_repr(value: Any) -> None:
    text = describe_value([value, len])
    if "..." not in text:
        assert text == f"[{value!r}, <function len>]"


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (lambda: 1, "<lambda>"),
        (len, "<function len>"),
        (to_json, "<function to_json>"),
        (str, "<class 'str'>"),
        (Widget, "<class 'Widget'>"),
        (object(), "<object object>"),
        ({"a": [len, {1, 2}], "b": ()}, "{'a': [<function len>, {1, 2}], 'b': ()}"),
        ((len,), "(<function len>,)"),
        (frozenset(), "frozenset()"),
        ([set(), len], "[set(), <function len>]"),
    ],
)
def test_describe_value(value: Any, text: str) -> None:
    assert describe_value(value) == text


@pytest.mark.parametrize(
    ("value", "name"),
    [
        (1, "int"),
        (True, "bool"),
        ("x", "str"),
        ([len], "list"),
        (None, "NoneType"),
        (len, "function"),
        (lambda: 1, "function"),
        (str, "type"),
        (Widget, "type"),
        (object(), "object"),
    ],
)
def test_describe_type(value: Any, name: str) -> None:
    assert describe_type(value) == name


def test_a_large_value_is_cut() -> None:
    assert describe_value("x" * 1000) == repr("x" * 200)[:200] + "..."
    assert len(describe_value(list(range(10_000)))) <= 203
    assert len(describe_value([*range(10_000), len])) <= 203


def test_a_deep_value_does_not_recurse_without_bound() -> None:
    deep: list[Any] = [len]
    for _ in range(sys.getrecursionlimit() * 2):
        deep = [deep]
    assert describe_value(deep).startswith("[[[")


def test_scrub_addresses() -> None:
    assert scrub_addresses(repr(to_json)) == "<function to_json>"
    assert scrub_addresses("KeyError: 'at 0x1'") == "KeyError: 'at 0x1'"
