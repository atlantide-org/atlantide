"""Atlas-lang subset validation: what the language refuses to admit."""

from __future__ import annotations

import pytest

from atlantide.core import LanguageError, is_successful
from atlantide.lang import validate_source

REJECTED = [
    ("while_loop", "while True:\n    pass"),
    ("class_def", "class Foo:\n    pass"),
    ("with_stmt", "with open('x') as f:\n    pass"),
    ("try_stmt", "try:\n    pass\nexcept Exception:\n    pass"),
    ("raise_stmt", "raise ValueError('x')"),
    ("async_def", "async def f():\n    pass"),
    ("yield_expr", "def f():\n    yield 1"),
    ("global_stmt", "def f():\n    global x"),
    ("delete_stmt", "x = 1\ndel x"),
    ("eval_call", "eval('1+1')"),
    ("exec_call", "exec('x=1')"),
    ("open_call", "open('/etc/passwd')"),
    ("dunder_name", "x = __import__"),
    ("dunder_attr", "x = (1).__class__"),
    ("getattr_call", "getattr(object, 'x')"),
    ("bad_import", "import os"),
    ("bad_import2", "import time"),
    ("bad_from_import", "from socket import socket"),
    ("relative_import", "from . import thing"),
    ("internal_lang_import", "from atlantide.lang.interp import importlib"),
    ("internal_lang_pkg", "import atlantide.lang.interp"),
    ("internal_ir_import", "from atlantide.ir import Encoder"),
]


@pytest.mark.parametrize("name, source", REJECTED, ids=[n for n, _ in REJECTED])
def test_rejected_constructs(name: str, source: str) -> None:
    result = validate_source(source)
    assert not is_successful(result), f"{name} should be rejected"
    assert isinstance(result.failure(), LanguageError)


ACCEPTED = [
    "x = 1 + 2 * 3",
    "y = [i for i in range(10) if i % 2 == 0]",
    "def f(a, b=2):\n    return a + b\n\nz = f(3)",
    "name = f'hello-{1 + 1}'",
    "from atlantide.core import Ref",
    # A published component mounts under this namespace; the sandbox must admit it
    # with no change (that is what makes the whole scheme work).
    "from atlantide.components.acme import SecureBucket",
    "import atlantide",
    "d = {k: v for k, v in [('a', 1)]}",
    "g = lambda x: x * 2",
    "t = sorted({3, 1, 2})",
]


@pytest.mark.parametrize("source", ACCEPTED)
def test_accepted_constructs(source: str) -> None:
    assert is_successful(validate_source(source))


def test_error_carries_position() -> None:
    result = validate_source("x = 1\nwhile True:\n    pass")
    err = result.failure()
    assert isinstance(err, LanguageError)
    assert err.line == 2


@pytest.mark.parametrize(
    "source, needle",
    [
        ("while True:\n    pass", "bounded `for`"),
        ("class Foo:\n    pass", "provider"),
        ("import json", "pure function"),
        ("getattr(x, 'y')", "determinism"),
    ],
)
def test_rejection_messages_are_actionable(source: str, needle: str) -> None:
    err = validate_source(source).failure()
    assert isinstance(err, LanguageError)
    assert needle in str(err)


def test_syntax_error_is_language_error() -> None:
    result = validate_source("def broken(:\n")
    assert not is_successful(result)
    assert isinstance(result.failure(), LanguageError)


# -- the one class config may declare --------------------------------------
#
# `ClassDef` is still absent from `_ALLOWED_NODES`: it is not a permitted node,
# only a permitted shape. Everything below is what that shape excludes.

_SCHEMA = "from atlantide.core import EnvSchema\n"

SCHEMA_REJECTED = [
    ("no_base", "class A:\n    x: str"),
    ("wrong_base", "class A(dict):\n    x: str"),
    ("two_bases", "class A(EnvSchema, dict):\n    x: str"),
    ("decorator", "@thing\nclass A(EnvSchema):\n    x: str"),
    ("metaclass", "class A(EnvSchema, metaclass=type):\n    x: str"),
    ("method", "class A(EnvSchema):\n    def f(self):\n        return 1"),
    ("bare_assign", "class A(EnvSchema):\n    x = 1"),
    ("nested_class", "class A(EnvSchema):\n    class B(EnvSchema):\n        x: str"),
    ("control_flow", "class A(EnvSchema):\n    if True:\n        x: str"),
    ("generic_annotation", "class A(EnvSchema):\n    x: list[str]"),
    ("unknown_type", "class A(EnvSchema):\n    x: Foo"),
    ("union_of_two", "class A(EnvSchema):\n    x: str | int"),
    ("underscore_field", "class A(EnvSchema):\n    _x: str"),
    ("reserved_field", "class A(EnvSchema):\n    name: str"),
    ("reserved_method_name", "class A(EnvSchema):\n    as_dict: str"),
    ("duplicate_field", "class A(EnvSchema):\n    x: str\n    x: int"),
    ("inside_loop", "for i in [1]:\n    class A(EnvSchema):\n        x: str"),
    ("inside_function", "def f():\n    class A(EnvSchema):\n        x: str"),
    # A default is an ordinary expression and must meet every other rule; this
    # holds only because `visit_ClassDef` recurses into defaults by hand.
    ("io_in_default", "class A(EnvSchema):\n    x: str = open('/etc/passwd')"),
    ("eval_in_default", "class A(EnvSchema):\n    x: str = eval('1')"),
    ("dunder_in_default", "class A(EnvSchema):\n    x: str = (1).__class__"),
]


@pytest.mark.parametrize("name, body", SCHEMA_REJECTED, ids=[n for n, _ in SCHEMA_REJECTED])
def test_schema_class_shape_is_enforced(name: str, body: str) -> None:
    result = validate_source(_SCHEMA + body)
    assert not is_successful(result), f"{name} should be rejected"
    assert isinstance(result.failure(), LanguageError)


SCHEMA_ACCEPTED = [
    "class A(EnvSchema):\n    x: str",
    "class A(EnvSchema):\n    '''A docstring is fine.'''\n    x: str",
    "class A(EnvSchema):\n    pass",
    "class A(EnvSchema):\n    x: str = 'a'\n    n: int = 1\n    b: bool = False",
    "class A(EnvSchema):\n    x: str | None = None",
    "class A(EnvSchema):\n    xs: list = []\n    d: dict = {}",
    # A default may be any ordinary config expression, exactly like `var(default=)`.
    "prefix = 'p'\nclass A(EnvSchema):\n    x: str = f'{prefix}-a'",
    "class A(EnvSchema):\n    x: str\nclass B(EnvSchema):\n    y: int",
]


@pytest.mark.parametrize("body", SCHEMA_ACCEPTED)
def test_schema_class_shapes_that_are_allowed(body: str) -> None:
    assert is_successful(validate_source(_SCHEMA + body))


@pytest.mark.parametrize(
    "body, needle",
    [
        ("class A:\n    x: str", "EnvSchema"),
        ("class A(EnvSchema):\n    def f(self):\n        return 1", "data, no behaviour"),
        ("class A(EnvSchema):\n    x = 1", "write 'x: str = 1' with a type"),
        ("class A(EnvSchema):\n    x: list[str]", "use `list`"),
        ("class A(EnvSchema):\n    _x: str", "read back as `env._x`"),
        ("class A(EnvSchema):\n    name: str", "collides"),
        ("for i in [1]:\n    class A(EnvSchema):\n        x: str", "module level"),
    ],
)
def test_schema_rejection_messages_are_actionable(body: str, needle: str) -> None:
    err = validate_source(_SCHEMA + body).failure()
    assert isinstance(err, LanguageError)
    assert needle in str(err)
