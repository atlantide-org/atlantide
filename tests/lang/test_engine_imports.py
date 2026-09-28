"""Engine machinery is not config surface.

`atlantide.core` re-exports the live resource registry, the `--env` selection
and the provider plumbing for the engine and CLI. Config binding any of them
reaches evaluation state outside the declaration path, so each is refused by
name at validation and by object identity where the interpreter binds it —
whatever module path, alias or re-export reached it. Names an allowed module
merely *imported* (`pathlib.Path`, `PathScope`, a plugin record) are refused by
where they were defined.
"""

from __future__ import annotations

import ast

import pytest

from atlantide.core import LanguageError, is_successful
from atlantide.lang import evaluate_source, validate_source
from atlantide.lang.builtins import build_globals
from atlantide.lang.interp import Interpreter, Scope
from atlantide.lang.validate import FORBIDDEN_CORE_NAMES


@pytest.mark.parametrize("name", sorted(FORBIDDEN_CORE_NAMES))
def test_engine_names_are_rejected_from_atlantide_core(name: str) -> None:
    source = f"from atlantide.core import {name}\n"
    assert not is_successful(validate_source(source))
    result = evaluate_source(source)
    assert not is_successful(result)
    assert "engine machinery" in str(result.failure()) or "provider" in str(result.failure())


SUBMODULE_PATHS = [
    "from atlantide.core.resource import active_registry",
    "from atlantide.core.resource import collecting",
    "from atlantide.core.resource import ResourceRegistry",
    "from atlantide.core.config import current_selection",
    "from atlantide.core.config import selecting",
    "from atlantide.core.config import EnvSelection",
    "from atlantide.core.inline import inline_stack_outputs",
    "from atlantide.core.registry import ProviderRegistry",
    "from atlantide.core.context import Context",
    "from atlantide.core import active_registry as r",
]


@pytest.mark.parametrize("source", SUBMODULE_PATHS)
def test_engine_names_are_rejected_from_submodules(source: str) -> None:
    assert not is_successful(validate_source(source))
    assert not is_successful(evaluate_source(source))


@pytest.mark.parametrize(
    "source",
    [
        "from atlantide.core import active_registry\nactive_registry()",
        "from atlantide.core.resource import active_registry as ar\nar()",
    ],
)
def test_interpreter_rejects_engine_objects_without_validation(source: str) -> None:
    """The identity check holds on the path that binds the name, alias or not."""
    with pytest.raises(LanguageError, match="engine machinery"):
        Interpreter().run(ast.parse(source), Scope(build_globals({})))


REEXPORT_ESCAPES = [
    # stdlib class imported by an allowed module: `Path(...).read_text()`.
    ("pathlib", "from atlantide.providers.aws.resources.compute import Path"),
    # resolves real paths from the cwd; re-exported by the local provider package.
    ("path_scope_reexport", "from atlantide.providers.local import PathScope"),
    ("path_scope_resources", "from atlantide.providers.local.resources import PathScope"),
    # a plugin record whose `factory({})` returns a live Provider.
    ("plugin_record", "from atlantide.providers.local import PLUGIN"),
    ("plugin_type", "from atlantide.providers.aws import ProviderPlugin"),
]


@pytest.mark.parametrize(("name", "source"), REEXPORT_ESCAPES, ids=[n for n, _ in REEXPORT_ESCAPES])
def test_names_an_allowed_module_merely_imported_are_rejected(name: str, source: str) -> None:
    result = evaluate_source(source + "\n")
    assert not is_successful(result), f"{name} should be rejected"


@pytest.mark.parametrize(
    "module",
    [
        "atlantide.core.logging",
        "atlantide.core.plugin",
        "atlantide.core.tuning",
        "atlantide.providers.loader",
        "atlantide.providers.local.paths",
        "atlantide.providers.aws.config",
    ],
)
def test_engine_plumbing_modules_are_not_importable(module: str) -> None:
    assert not is_successful(validate_source(f"from {module} import X\n"))


def test_path_read_through_a_reexport_is_refused() -> None:
    source = (
        "from atlantide.providers.aws.resources.compute import Path\n"
        "x = Path('/etc/hosts').read_text()\n"
    )
    result = evaluate_source(source)
    assert not is_successful(result)
    assert "pathlib" in str(result.failure())


LEGIT = [
    "from atlantide.core import Config, EnvSchema, Stack, output, var, secret\n",
    "from atlantide.core import interpolate, join, concat, mutable, immutable, computed\n",
    "from atlantide.core import current_stack, current_stack_region, current_stack_tags\n",
    "from atlantide.core import current_stack_name_prefix, current_config, region\n",
    "from atlantide.core import Lifecycle, Ref, SecretRef, StackReference, Component, child\n",
    "from atlantide.core import Result, Success, Failure, is_successful, UNSET\n",
    "from atlantide.core.resource import output\n",
    "from atlantide.policy import enforce\n",
    "from atlantide.providers.aws import S3Bucket\n",
    "from atlantide.providers.local import File, TYPES\n",
    "from atlantide.providers.random import Id, Password\n",
]


@pytest.mark.parametrize("source", LEGIT)
def test_config_surface_still_binds(source: str) -> None:
    assert is_successful(validate_source(source))
    result = evaluate_source(source)
    assert is_successful(result), result
