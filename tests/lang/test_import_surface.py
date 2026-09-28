"""The frozen import surface: containers config can import hold only config API.

The interpreter binds a module-level list/dict/tuple/set as plain data without
looking inside, so :func:`atlantide.lang.surface.audit_import_surface` is what
keeps a dangerous element (a handler, a ``Path``, an engine function) out of
reach. Running it over the installed tree here makes such a container a CI
failure.
"""

from __future__ import annotations

import collections
import os
import pathlib
import sys
import types

import pytest

from atlantide.core import is_successful
from atlantide.core.errors import LanguageError
from atlantide.lang import LanguageSurface, evaluate_source
from atlantide.lang.surface import audit_import_surface, audit_module, importable_modules

FIXTURE = "atlas_surface_fixture"
FIXTURE_SURFACE = LanguageSurface(extra=frozenset({FIXTURE}))


def test_installed_surface_has_no_violations() -> None:
    violations = audit_import_surface()
    assert violations == [], "\n".join(
        f"{v.module}.{v.path}: {v.type_name} — {v.reason}" for v in violations
    )


def test_walk_covers_the_allowed_tree_and_skips_internal_modules() -> None:
    modules = importable_modules()
    assert {"atlantide.core", "atlantide.policy", "atlantide.providers.aws"} <= set(modules)
    assert not [m for m in modules if {"provider", "handlers"} & set(m.split("."))]
    assert "atlantide.components.fetch" not in modules


def test_aws_handler_registry_is_not_importable() -> None:
    # Guards against `atlantide.providers.aws` re-exporting the handler registry,
    # whose values make the boto3 calls.
    result = evaluate_source("from atlantide.providers.aws import HANDLERS\n")
    assert not is_successful(result)
    assert isinstance(result.failure(), LanguageError)


@pytest.fixture
def fixture_module(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    module = types.ModuleType(FIXTURE)

    def surface_fn() -> None:
        """A function config may import: defined in the fixture module."""

    surface_fn.__module__ = FIXTURE
    cycle: list[object] = [1]
    cycle.append(cycle)
    module.__dict__.update(
        SAFE={"a": [1, 2.0, ("b", None, b"x")], frozenset({"k"}): {True}},
        SAFE_FN=[surface_fn],
        CYCLE=cycle,
        DEEP_PATH=[1, {"k": (pathlib.Path("/"),)}],
        BAD_KEY={os.system: 1},
        BAD_SET={os.getcwd},
        BAD_SUBCLASS=collections.OrderedDict(a=1),
        BAD_MODULE=(os,),
        _PRIVATE=[pathlib.Path("/")],
        NOT_A_CONTAINER=pathlib.Path("/"),
    )
    monkeypatch.setitem(sys.modules, FIXTURE, module)
    return module


def test_synthetic_violations_are_detected(fixture_module: types.ModuleType) -> None:
    found = {(v.path, v.type_name) for v in audit_module(fixture_module, FIXTURE_SURFACE)}
    path_type = type(pathlib.Path("/"))
    assert found == {
        ("DEEP_PATH[1]['k'][0]", f"{path_type.__module__}.{path_type.__qualname__}"),
        (f"BAD_KEY{{{os.system!r}}}", "builtins.builtin_function_or_method"),
        (f"BAD_SET{{{os.getcwd!r}}}", "builtins.builtin_function_or_method"),
        ("BAD_SUBCLASS", "collections.OrderedDict"),
        ("BAD_MODULE[0]", "builtins.module"),
    }


def test_audit_reaches_extra_surface_modules(fixture_module: types.ModuleType) -> None:
    assert FIXTURE in importable_modules(FIXTURE_SURFACE)
    assert {v.module for v in audit_import_surface(FIXTURE_SURFACE)} == {FIXTURE}
