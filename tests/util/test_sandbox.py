"""`atlantide.util` is engine plumbing and must stay unreachable from config.

Its helpers create files and build AWS clients. The import allow-list in
`atlantide.lang.validate` admits `core`, `policy`, `providers` and `components`
only; these tests fail if `util` ever lands under one of them or the list grows
to include it.
"""

from __future__ import annotations

import importlib

import pytest
from returns.result import Failure

import atlantide.util
from atlantide.lang.interp import bind_rejection
from atlantide.lang.validate import DEFAULT_SURFACE, import_allowed, validate_source
from atlantide.util.fs import write_private

_MODULES = [
    "atlantide.util",
    "atlantide.util.aws",
    "atlantide.util.errors",
    "atlantide.util.fs",
    "atlantide.util.jsonfmt",
    "atlantide.util.project",
]


@pytest.mark.parametrize("module", _MODULES)
def test_util_modules_are_not_importable_from_config(module: str) -> None:
    importlib.import_module(module)  # the module exists; the list is not stale
    assert import_allowed(module, DEFAULT_SURFACE) is False


def test_the_validator_rejects_a_util_import() -> None:
    result = validate_source("from atlantide.util.fs import write_private\n")
    assert isinstance(result, Failure)


def test_a_util_object_re_exported_elsewhere_does_not_bind() -> None:
    """Even if an allowed module imported a util helper, config could not take it."""
    assert bind_rejection(write_private, "write_private", "atlantide.core") is not None
    assert bind_rejection(atlantide.util, "util", "atlantide") is not None
