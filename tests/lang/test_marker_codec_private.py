"""The shared handle-to-marker rebuild is implementation, never config surface.

`handles_to_markers` lives in the private `atlantide.core._tree` module; the
public `core.types` and `core.markers` modules call it through that module
(`_tree.handles_to_markers`) rather than binding it, so it is neither importable
by its own path nor reachable as an attribute of a config-importable module.
"""

from __future__ import annotations

import pytest

import atlantide.core.markers
import atlantide.core.types
from atlantide.core import is_successful
from atlantide.core.errors import LanguageError
from atlantide.lang import evaluate_source, validate_source


def test_handles_to_markers_by_its_module_path_is_rejected() -> None:
    source = "from atlantide.core._tree import handles_to_markers\n"
    for result in (validate_source(source), evaluate_source(source)):
        assert not is_successful(result)
        error = result.failure()
        assert isinstance(error, LanguageError)
        assert "import from 'atlantide.core._tree' is not allowed" in str(error)


@pytest.mark.parametrize("module", ["atlantide.core.types", "atlantide.core.markers"])
@pytest.mark.parametrize("name", ["handles_to_markers", "_to_markers", "_tree"])
def test_the_codec_is_not_reachable_through_a_public_module(module: str, name: str) -> None:
    result = evaluate_source(f"from {module} import {name}\n")
    assert not is_successful(result)
    assert isinstance(result.failure(), LanguageError)


@pytest.mark.parametrize("module", [atlantide.core.types, atlantide.core.markers])
def test_the_codec_is_not_bound_in_a_public_module(module: object) -> None:
    assert not hasattr(module, "handles_to_markers")
    assert not hasattr(module, "_to_markers")
