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
