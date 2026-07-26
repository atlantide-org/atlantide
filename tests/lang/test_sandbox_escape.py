"""The sandbox boundary: what config must not be able to reach.

Each test names the capability being denied — environment, disk, subprocess, the
interpreter's own globals — rather than the syntax that reaches it, since the
subset rules in :mod:`atlantide.lang.validate` are worth exactly what this file
proves about them.
"""

from __future__ import annotations

import pytest

from atlantide.core import LanguageError, is_successful
from atlantide.core.errors import LanguageError as CoreLanguageError
from atlantide.core.types import format_template, interpolate
from atlantide.lang import evaluate_source, validate_source

#: (capability, source). Denied by the import allow-list: each target module is
#: ordinary unsandboxed Python importing `os`, `pathlib`, or `subprocess`, and the
#: interpreter binds what it finds there into config scope as a live callable.
ESCAPES = [
    ("environ", "from atlantide.secrets.env import EnvSecretsProvider"),
    ("file_read", "from atlantide.providers.local.provider import _read_content"),
    ("subprocess", "from atlantide.components.fetch import _git"),
    ("secret_store", "from atlantide.secrets.keyfile_store import KeyfileValueStore"),
    ("state_write", "from atlantide.state.sqlite_backend import SqliteStateBackend"),
    ("cli", "from atlantide.cli.main import app"),
    ("component_lock", "from atlantide.components.lock import load_lock"),
    ("aws_handler", "from atlantide.providers.aws.handlers.s3 import S3BucketHandler"),
    ("private_name", "from atlantide.core.markers import _tree"),
]


@pytest.mark.parametrize("capability, source", ESCAPES, ids=[c for c, _ in ESCAPES])
def test_import_escapes_are_rejected(capability: str, source: str) -> None:
    result = validate_source(source)
    assert not is_successful(result), f"{capability} escape should be rejected"
    assert isinstance(result.failure(), LanguageError)


def test_provider_classes_are_not_importable_by_config() -> None:
    """`atlantide.providers.aws` is allow-listed for its resource types, but a
    Provider is the object holding the boto3 calls."""
    result = evaluate_source("from atlantide.providers.aws import AwsProvider\nx = 1")
    assert not is_successful(result)
    assert "provider" in str(result.failure()).lower()


def test_interpreter_rejects_a_denied_module_even_without_validation() -> None:
    """The allow-list is re-checked where the name is actually bound.

    `import_module` executes the target and `getattr` hands config a live
    callable, so `_st_ImportFrom` cannot rely on having been validated first.
    """
    from atlantide.lang.builtins import build_globals
    from atlantide.lang.interp import Interpreter, Scope

    tree = __import__("ast").parse("from atlantide.secrets.env import EnvSecretsProvider")
    with pytest.raises(CoreLanguageError):
        Interpreter().run(tree, Scope(build_globals({})))


# -- format-string traversal ------------------------------------------------
#
# `"{0.__class__.__init__.__globals__[x]}".format(obj)` walks attributes on a
# live object. The template is an `ast.Constant`, so the dunder ban on Name and
# Attribute nodes does not see it.

_TRAVERSAL = "{0.__class__.__init__.__globals__[__builtins__]}"


def test_str_format_is_not_callable_from_config() -> None:
    result = validate_source(f"x = '{_TRAVERSAL}'.format(1)")
    assert not is_successful(result)
    assert "format" in str(result.failure())


def test_interpolate_rejects_a_traversing_template_at_config_time() -> None:
    with pytest.raises(CoreLanguageError):
        interpolate(_TRAVERSAL, object())


@pytest.mark.parametrize("template", [_TRAVERSAL, "{0[0]}", "{0.real}"])
def test_format_template_rejects_field_access(template: str) -> None:
    """The apply-time reducer is the second half: a `$transform` marker read back
    from state has not passed `interpolate`'s config-time check."""
    with pytest.raises(CoreLanguageError):
        format_template(template, object())


def test_format_template_still_substitutes_positionally() -> None:
    assert format_template("{}/img/{}", "cdn", "logo.png") == "cdn/img/logo.png"
    assert format_template("{1}-{0}", "b", "a") == "a-b"


def test_format_template_reports_a_missing_argument() -> None:
    with pytest.raises(CoreLanguageError):
        format_template("{0}/{1}", "only-one")


# -- silently discarded arguments -------------------------------------------
#
# Not a sandbox hole but the same failure shape: the config compiles, and the
# plan matches what was written minus the dropped values, with no diagnostic.
# `Bucket("assets", bucket=name, **common_tags)` yields a bucket with no tags.

DISCARDED = [
    ("kwargs_unpacking", "def h(a=1):\n    return a\nx = h(**{'a': 9})"),
    ("surplus_positional", "def f(a):\n    return a\nx = f(1, 2, 3)"),
]


@pytest.mark.parametrize("name, source", DISCARDED, ids=[n for n, _ in DISCARDED])
def test_silently_discarded_arguments_are_rejected(name: str, source: str) -> None:
    result = evaluate_source(source)
    assert not is_successful(result), f"{name} should be rejected"


@pytest.mark.parametrize(
    "source",
    [
        "def g(a, b=2):\n    return a + b\nx = g(1)",
        "def g(a, b=2):\n    return a + b\nx = g(1, 5)",
        "def g(a, b=2):\n    return a + b\nx = g(1, b=5)",
        "def g(a, b=2):\n    return a + b\nx = g(*[1, 5])",
    ],
)
def test_ordinary_calls_still_work(source: str) -> None:
    assert is_successful(evaluate_source(source))


# -- the surface config legitimately needs must keep working ----------------

ALLOWED = [
    "from atlantide.core import Stack, output",
    "from atlantide.policy import enforce",
    "from atlantide.providers.aws import S3Bucket, SecureBucket",
    "from atlantide.providers.random import Id",
    "from atlantide.components.acme import SecureSite",
]


@pytest.mark.parametrize("source", ALLOWED)
def test_config_surface_is_still_importable(source: str) -> None:
    assert is_successful(validate_source(source))
