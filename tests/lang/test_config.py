"""``Config`` through the interpreter: reachability, `--env`, and error locations.

A config declares its environments one of two ways: an ``EnvSchema`` subclass —
the one class Atlas-lang admits, and the form an editor can complete — or a
mapping of ``var()`` declarations. Both are imported from ``atlantide.core`` and
validated at construction. This file drives the whole path a real config takes
for both: the import allow-list, attribute access under the interpreter, and the
failure text an author actually sees.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from returns.result import Failure, Success

from atlantide.core import LanguageError, Resource, immutable, mutable
from atlantide.lang import evaluate_source, validate_source


class Widget(Resource):
    """Test resource injected via extra_globals (no provider package needed)."""

    class Meta:
        provider: ClassVar[str] = "test"

    size: int = immutable()
    label: str = mutable(default="")


HEADER = "from atlantide.core import Config, Stack, var\n"

CONFIG = HEADER + (
    "config = Config(\n"
    "    schema={'size': var(int, default=1), 'domain': var(str)},\n"
    "    envs={\n"
    "        'dev':  {'region': 'eu-north-1', 'domain': 'dev.x.io'},\n"
    "        'prod': {'region': 'us-east-1', 'domain': 'x.io', 'size': 5},\n"
    "    },\n"
    ")\n"
    "for env in config.envs():\n"
    "    with Stack(env.name, config=env):\n"
    "        Widget('w', size=env.size, label=env.domain)\n"
)


def _run(source: str, **kw: Any) -> Any:
    return evaluate_source(source, extra_globals={"Widget": Widget}, **kw)


def _ids(source: str, **kw: Any) -> list[str]:
    return [r.node_id for r in _run(source, **kw).unwrap().all()]


def _error(source: str, **kw: Any) -> LanguageError:
    result = _run(source, **kw)
    assert isinstance(result, Failure), result
    error = result.failure()
    assert isinstance(error, LanguageError), error
    return error


# -- reachability ------------------------------------------------------------


def test_config_and_var_are_importable_from_config() -> None:
    """The import allow-list already covers `atlantide.core`; an injected global
    would be overwritten by the very `from ... import` that names it."""
    assert isinstance(validate_source(CONFIG), Success)
    assert _ids(CONFIG) == ["dev:test.Widget:w", "prod:test.Widget:w"]


def test_an_environment_value_reaches_a_resource_field() -> None:
    registry = _run(CONFIG).unwrap()
    assert registry.get("prod:test.Widget:w").unwrap().size == 5
    assert registry.get("dev:test.Widget:w").unwrap().size == 1  # the default


def test_the_stack_takes_its_region_from_the_environment() -> None:
    source = HEADER + (
        "config = Config(envs={'dev': {'region': 'eu-north-1', 'tags': {'env': 'dev'}}})\n"
        "for env in config.envs():\n"
        "    with Stack(env.name, config=env):\n"
        "        Widget('w', size=1)\n"
    )
    assert _ids(source) == ["dev:test.Widget:w"]


def test_subscript_reads_an_environment_variable() -> None:
    source = HEADER + (
        "config = Config(schema={'domain': var(str)}, "
        "envs={'dev': {'region': 'r', 'domain': 'd'}})\n"
        "for env in config.envs():\n"
        "    with Stack(env.name, config=env):\n"
        "        Widget('w', size=1, label=env['domain'])\n"
    )
    registry = _run(source).unwrap()
    assert registry.get("dev:test.Widget:w").unwrap().label == "d"


def test_an_environment_value_may_be_built_from_an_input() -> None:
    """The two mechanisms compose: what differs between environments is in the
    file, what differs between runs of one environment is an input."""
    source = HEADER + (
        "base = atlantide.input('base')\n"
        "config = Config(schema={'domain': var(str)}, "
        "envs={'dev': {'region': 'r', 'domain': f'dev.{base}'}})\n"
        "for env in config.envs():\n"
        "    with Stack(env.name, config=env):\n"
        "        Widget('w', size=1, label=env.domain)\n"
    )
    registry = _run(source, inputs={"base": "x.io"}).unwrap()
    assert registry.get("dev:test.Widget:w").unwrap().label == "dev.x.io"
    assert registry.inputs == {"base": "x.io"}


def test_a_dunder_on_an_environment_is_rejected_before_evaluation() -> None:
    source = CONFIG + "label = config.__class__\n"
    result = validate_source(source)
    assert isinstance(result, Failure)
    assert "dunder attribute" in str(result.failure())


# -- environment selection ---------------------------------------------------


def test_env_narrows_which_stacks_are_declared() -> None:
    assert _ids(CONFIG, envs=["prod"]) == ["prod:test.Widget:w"]


def test_no_selection_means_every_environment() -> None:
    assert _ids(CONFIG, envs=None) == ["dev:test.Widget:w", "prod:test.Widget:w"]


def test_several_environments_may_be_selected() -> None:
    assert _ids(CONFIG, envs=["prod", "dev"]) == ["dev:test.Widget:w", "prod:test.Widget:w"]


def test_the_registry_records_what_was_declared_and_what_was_selected() -> None:
    registry = _run(CONFIG, envs=["prod"]).unwrap()
    assert registry.envs_declared == ("dev", "prod")
    assert registry.envs_selected == ("prod",)


def test_an_unknown_environment_names_the_declared_ones() -> None:
    """A typo must not read as a successful run that did nothing."""
    error = _error(CONFIG, envs=["prd"])
    assert "unknown environment 'prd'" in str(error)
    assert "dev, prod" in str(error)


def test_env_against_a_config_with_no_config_is_reported() -> None:
    source = "from atlantide.core import Stack\nwith Stack('s', region='r'):\n    pass\n"
    error = _error(source, envs=["prod"])
    assert "--env was given but the config declares no Config" in str(error)


def test_a_second_config_is_refused() -> None:
    source = CONFIG + "other = Config(envs={'qa': {'region': 'r'}})\n"
    assert "more than one Config()" in str(_error(source))


# -- error reporting ---------------------------------------------------------


def test_a_config_error_carries_the_line_it_was_written_on() -> None:
    """A native callable has no view of the source, so without the interpreter
    stamping the call site this renders with no caret."""
    source = HEADER + (
        "config = Config(\n"
        "    schema={'domain': var(str)},\n"
        "    envs={'dev': {'region': 'r'}},\n"
        ")\n"
    )
    error = _error(source)
    assert "missing required variable 'domain'" in str(error)
    assert error.line == 2  # the `Config(` call, not the end of the literal


def test_a_type_error_in_one_environment_fails_the_whole_evaluation() -> None:
    """A prod-only mistake fails `validate` in CI, not the prod apply."""
    source = HEADER + (
        "config = Config(schema={'size': var(int, default=1)}, "
        "envs={'dev': {'region': 'r'}, 'prod': {'region': 'r', 'size': 'five'}})\n"
    )
    error = _error(source)
    assert "environment 'prod': variable 'size' expects int" in str(error)


def test_reading_an_undeclared_variable_names_the_environment() -> None:
    source = CONFIG.replace("label=env.domain", "label=env.doman")
    error = _error(source)
    assert "environment 'dev' has no variable 'doman'" in str(error)
    assert "domain" in str(error)


def test_a_non_string_region_is_caught_before_it_reaches_a_resource() -> None:
    source = HEADER + (
        "config = Config(schema={'region': var(int)}, envs={'dev': {'region': 5}})\n"
        "for env in config.envs():\n"
        "    with Stack(env.name, config=env):\n"
        "        Widget('w', size=1)\n"
    )
    assert "'region' must be str" in str(_error(source))


# -- the EnvSchema class form ------------------------------------------------

SCHEMA_HEADER = "from atlantide.core import Config, EnvSchema, Stack\n"

CLASS_CONFIG = SCHEMA_HEADER + (
    "class AppEnv(EnvSchema):\n"
    "    domain: str\n"
    "    size: int = 1\n"
    "    cert: str | None = None\n"
    "config = Config(AppEnv, envs={\n"
    "    'dev':  {'region': 'eu-north-1', 'domain': 'dev.x.io'},\n"
    "    'prod': {'region': 'us-east-1', 'domain': 'x.io', 'size': 5},\n"
    "})\n"
    "for env in config.envs():\n"
    "    with Stack(env.name, config=env):\n"
    "        Widget('w', size=env.size, label=env.domain)\n"
)


def test_a_declared_schema_reaches_a_resource_field() -> None:
    registry = _run(CLASS_CONFIG).unwrap()
    assert [r.node_id for r in registry.all()] == ["dev:test.Widget:w", "prod:test.Widget:w"]
    assert registry.get("prod:test.Widget:w").unwrap().size == 5
    assert registry.get("dev:test.Widget:w").unwrap().size == 1  # the default


def test_a_declared_schema_takes_the_stacks_region_from_the_environment() -> None:
    registry = _run(CLASS_CONFIG).unwrap()
    assert registry.get("prod:test.Widget:w").unwrap().stack == "prod"


def test_env_narrows_a_declared_schema_too() -> None:
    assert _ids(CLASS_CONFIG, envs=["prod"]) == ["prod:test.Widget:w"]


def test_a_nullable_field_defaults_to_none() -> None:
    source = SCHEMA_HEADER + (
        "class AppEnv(EnvSchema):\n"
        "    cert: str | None\n"
        "config = Config(AppEnv, envs={'dev': {'region': 'r'}})\n"
        "for env in config.envs():\n"
        "    with Stack(env.name, config=env):\n"
        "        Widget('w', size=1, label=str(env.cert))\n"
    )
    registry = _run(source).unwrap()
    assert registry.get("dev:test.Widget:w").unwrap().label == "None"


def test_a_missing_required_field_names_the_environment() -> None:
    source = SCHEMA_HEADER + (
        "class AppEnv(EnvSchema):\n"
        "    domain: str\n"
        "config = Config(AppEnv, envs={'dev': {'region': 'r'}})\n"
    )
    assert "environment 'dev' is missing required variable 'domain'" in str(_error(source))


def test_a_prod_only_type_error_fails_the_whole_evaluation() -> None:
    source = SCHEMA_HEADER + (
        "class AppEnv(EnvSchema):\n"
        "    size: int = 1\n"
        "config = Config(AppEnv, envs={'dev': {'region': 'r'}, "
        "'prod': {'region': 'r', 'size': 'five'}})\n"
    )
    assert "environment 'prod': variable 'size' expects int" in str(_error(source))


def test_a_typo_on_a_declared_schema_still_names_the_environment() -> None:
    """The runtime message is what a generated dataclass would have lost."""
    source = CLASS_CONFIG.replace("env.domain", "env.doman")
    error = _error(source)
    assert "environment 'dev' has no variable 'doman'" in str(error)
    assert "domain" in str(error)


def test_an_unknown_variable_is_still_rejected_against_a_declared_schema() -> None:
    source = SCHEMA_HEADER + (
        "class AppEnv(EnvSchema):\n"
        "    domain: str\n"
        "config = Config(AppEnv, envs={'dev': {'region': 'r', 'domain': 'd', 'extra': 1}})\n"
    )
    assert "unknown variable 'extra'" in str(_error(source))


def test_a_schema_class_error_carries_the_line_it_was_written_on() -> None:
    source = SCHEMA_HEADER + (
        "class AppEnv(EnvSchema):\n"
        "    domain: str\n"
        "config = Config(\n"
        "    AppEnv,\n"
        "    envs={'dev': {'region': 'r'}},\n"
        ")\n"
    )
    assert _error(source).line == 4  # the `Config(` call


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
