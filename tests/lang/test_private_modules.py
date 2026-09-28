"""A leading-underscore module is never config surface.

`atlantide.core._describe` (the error-message renderer) and `atlantide.core._tree`
live under an allowed prefix, but they are the package's own implementation:
config may not import them by path, nor bind what they define through a public
module that imported it (`interp.binding` checks where the object was defined).
"""

from __future__ import annotations

import pytest

import atlantide.core
from atlantide.core import is_successful
from atlantide.core.errors import LanguageError
from atlantide.lang import evaluate_source, validate_source
from atlantide.lang.surface import importable_modules
from atlantide.lang.validate import import_allowed


@pytest.mark.parametrize(
    "module",
    [
        "atlantide.core._describe",
        "atlantide.core._tree",
        "atlantide.components._layout",
        "atlantide._private",
        "atlantide.providers._x.y",
    ],
)
def test_a_private_module_segment_is_not_importable(module: str) -> None:
    assert not import_allowed(module)


def test_describe_by_its_module_path_is_rejected() -> None:
    source = "from atlantide.core._describe import describe_value\n"
    for result in (validate_source(source), evaluate_source(source)):
        assert not is_successful(result)
        error = result.failure()
        assert isinstance(error, LanguageError)
        assert "import from 'atlantide.core._describe' is not allowed" in str(error)


def test_describe_is_not_exported_from_core() -> None:
    assert not hasattr(atlantide.core, "describe_value")
    result = evaluate_source("from atlantide.core import describe_value\n")
    assert not is_successful(result)
    assert isinstance(result.failure(), LanguageError)


@pytest.mark.parametrize(
    ("module", "name"),
    [
        ("atlantide.core.config", "describe_value"),
        ("atlantide.core.stack", "describe_type"),
        ("atlantide.components", "components_dir"),
    ],
)
def test_a_private_definition_reexported_by_a_public_module_is_rejected(
    module: str, name: str
) -> None:
    result = evaluate_source(f"from {module} import {name}\n")
    assert not is_successful(result)
    assert "which is not config API" in str(result.failure())


def test_the_surface_audit_skips_private_modules() -> None:
    modules = importable_modules()
    assert not [m for m in modules if any(part.startswith("_") for part in m.split("."))]
    assert "atlantide.core.config" in modules
