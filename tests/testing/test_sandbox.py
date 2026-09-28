"""``atlantide.testing`` is for component tests, never for config.

It imports the engine (compile, diff, provider discovery), so the import allow-list
must keep rejecting it, including once the module is loaded in the process.
"""

from __future__ import annotations

import pytest
from returns.result import Failure

import atlantide.testing  # noqa: F401  (loaded, so the rejection is not an ImportError)
from atlantide.lang import evaluate_source, validate_source
from atlantide.lang.validate import DEFAULT_SURFACE, import_allowed

_SOURCES = [
    "from atlantide.testing import Compiled\n",
    "from atlantide.testing.compiled import Compiled\n",
    "import atlantide.testing\n",
    "from atlantide import testing\n",
]


@pytest.mark.parametrize("module", ["atlantide.testing", "atlantide.testing.compiled"])
def test_testing_is_outside_the_import_surface(module: str) -> None:
    assert import_allowed(module, DEFAULT_SURFACE) is False


@pytest.mark.parametrize("source", _SOURCES)
def test_config_cannot_import_testing(source: str) -> None:
    assert isinstance(evaluate_source(source), Failure)


def test_the_validator_rejects_a_testing_import() -> None:
    assert isinstance(validate_source("from atlantide.testing import Compiled\n"), Failure)
