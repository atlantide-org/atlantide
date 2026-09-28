"""Regressions for ``Config``/``EnvSchema`` and node-id validation fixes."""

from __future__ import annotations

from typing import Any

import pytest

from atlantide.core import Config, EnvSchema, LanguageError, var
from atlantide.core.config import selecting
from atlantide.core.errors import RegistryError
from atlantide.core.node_id import require_identifier

ENVS: dict[str, dict[str, Any]] = {"dev": {"region": "a"}, "prod": {"region": "b"}}


def _schema(source: str) -> Any:
    """Evaluate a class body without inheriting this file's postponed annotations."""
    namespace: dict[str, Any] = {"EnvSchema": EnvSchema}
    exec(compile(source, "<schema>", "exec", dont_inherit=True), namespace)
    return namespace["S"]


# -- selection without envs() ------------------------------------------------


def test_a_config_read_only_through_env_selects_every_environment() -> None:
    """Without it `selected` stayed `()`, so every declared environment was
    excluded and its resources planned as unchanged."""
    with selecting(None) as selection:
        Config(envs=ENVS).env("prod")
    assert selection.selected == ("dev", "prod")


def test_a_config_read_only_through_env_honours_the_selection() -> None:
    with selecting(["prod"]) as selection:
        Config(envs=ENVS).env("prod")
    assert selection.selected == ("prod",)


def test_an_unknown_env_is_reported_even_if_envs_is_never_called() -> None:
    with selecting(["typo"]), pytest.raises(LanguageError, match="unknown environment 'typo'"):
        Config(envs=ENVS)


def test_envs_still_records_the_selection() -> None:
    with selecting(["dev"]) as selection:
        config = Config(envs=ENVS)
        assert [env.name for env in config.envs()] == ["dev"]
    assert selection.selected == ("dev",)


# -- nullability with a default ---------------------------------------------


def test_an_optional_field_with_a_default_accepts_none() -> None:
    class S(EnvSchema):
        cert: str | None = "x"

    config = Config(S, envs={"dev": {"cert": None}, "prod": {}})
    assert config.env("dev").cert is None
    assert config.env("prod").cert == "x"


def test_a_non_optional_field_with_a_default_still_rejects_none() -> None:
    class S(EnvSchema):
        cert: str = "x"

    with pytest.raises(LanguageError, match="variable 'cert' expects str"):
        Config(S, envs={"dev": {"cert": None}})


# -- evaluated (non-postponed) annotations -----------------------------------


@pytest.mark.parametrize("annotation", ["list[str]", "dict[str, int]", "list[str] | None"])
def test_an_evaluated_generic_is_refused_as_a_generic(annotation: str) -> None:
    with pytest.raises(LanguageError, match="parameterised generics"):
        _schema(f"class S(EnvSchema):\n    subnets: {annotation}\n")


def test_an_evaluated_optional_is_still_accepted() -> None:
    schema = _schema("class S(EnvSchema):\n    cert: str | None\n")
    assert schema.__atlas_fields__ == {"cert": var(str, default=None)}


def test_evaluated_annotations_are_collected() -> None:
    """On Python 3.14 (PEP 649) `cls.__dict__` has no `__annotations__`."""
    schema = _schema("class S(EnvSchema):\n    size: int\n    tier: str = 'a'\n")
    assert schema.__atlas_fields__ == {"size": var(int), "tier": var(str, default="a")}


def test_a_child_schema_inherits_its_parents_fields() -> None:
    class Base(EnvSchema):
        size: int
        tier: str = "a"

    class Child(Base):
        tier: str = "b"  # type: ignore[assignment]
        extra: bool = False

    assert Child.__atlas_fields__ == {
        "size": var(int),
        "tier": var(str, default="b"),
        "extra": var(bool, default=False),
    }
    env = Config(Child, envs={"dev": {"size": 1}}).env("dev")
    assert (env.size, env.tier, env.extra) == (1, "b", False)


# -- names -------------------------------------------------------------------


@pytest.mark.parametrize("name", ["class", "None", "lambda"])
def test_a_keyword_variable_name_is_refused(name: str) -> None:
    """`env.class` is a syntax error, so the variable could never be read."""
    with pytest.raises(LanguageError, match="keyword"):
        Config(schema={name: var(str, default="x")}, envs={"dev": {}})


@pytest.mark.parametrize("name", ["web\n", "web\nx", "wéb", "web٣"])
def test_an_identifier_must_match_in_full_and_in_ascii(name: str) -> None:
    with pytest.raises(RegistryError, match="invalid stack name"):
        require_identifier(name, "stack")


def test_an_environment_name_with_a_trailing_newline_is_refused() -> None:
    with pytest.raises(RegistryError):
        Config(envs={"dev\n": {}})
