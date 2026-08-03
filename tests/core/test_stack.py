"""Stacks: node_id namespacing, nesting, and default behaviour."""

from __future__ import annotations

from typing import ClassVar

import pytest

from atlantide.core import (
    Config,
    EnvView,
    LanguageError,
    Ref,
    RegistryError,
    Resource,
    Stack,
    collecting,
    computed,
    current_config,
    current_stack,
    immutable,
    mutable,
    var,
)


class Thing(Resource):
    class Meta:
        provider: ClassVar[str] = "test"

    size: int = immutable()


class Tagged(Resource):
    class Meta:
        provider: ClassVar[str] = "test"

    size: int = immutable()
    tags: dict[str, str] = mutable(default_factory=dict)


def test_default_stack() -> None:
    t = Thing("a", size=1)
    assert t.stack == "default"
    assert t.node_id == "default:test.Thing:a"


def test_stack_namespaces_node_id() -> None:
    with Stack("prod", region="us-east-1"):
        t = Thing("a", size=1)
    assert t.stack == "prod"
    assert t.node_id == "prod:test.Thing:a"


def test_same_name_across_stacks_does_not_collide() -> None:
    with collecting() as reg:
        with Stack("dev", region="us-east-1"):
            Thing("logs", size=1)
        with Stack("prod", region="us-east-1"):
            Thing("logs", size=2)
    assert {r.node_id for r in reg.all()} == {
        "dev:test.Thing:logs",
        "prod:test.Thing:logs",
    }


def test_nested_stacks_innermost_wins() -> None:
    with Stack("outer", region="us-east-1"):
        assert current_stack() == "outer"
        with Stack("inner", region="us-east-1"):
            assert current_stack() == "inner"
            t = Thing("x", size=1)
        assert current_stack() == "outer"
    assert t.node_id == "inner:test.Thing:x"
    assert current_stack() == "default"


def test_invalid_stack_name() -> None:
    with pytest.raises(RegistryError, match="invalid stack name"):
        Stack("bad name", region="us-east-1")


def test_stack_tags_merge_into_resource() -> None:
    with Stack("prod", region="us-east-1", tags={"env": "prod", "team": "infra"}):
        t = Tagged("a", size=1, tags={"app": "web"})
    assert t.tags == {"env": "prod", "team": "infra", "app": "web"}


def test_resource_tags_win_on_conflict() -> None:
    with Stack("prod", region="us-east-1", tags={"env": "prod"}):
        t = Tagged("a", size=1, tags={"env": "override"})
    assert t.tags == {"env": "override"}


def test_nested_stack_tags_merge_inner_wins() -> None:
    with Stack("outer", region="us-east-1", tags={"a": "1", "b": "outer"}):  # noqa: SIM117 - testing nesting
        with Stack("inner", region="us-east-1", tags={"b": "inner", "c": "3"}):
            t = Tagged("x", size=1)
    assert t.tags == {"a": "1", "b": "inner", "c": "3"}


def test_stack_tags_ignored_when_no_tags_field() -> None:
    with Stack("prod", region="us-east-1", tags={"env": "prod"}):
        t = Thing("a", size=1)  # no tags field -> unaffected, no error
    assert t.node_id == "prod:test.Thing:a"


def test_no_stack_tags_leaves_resource_untouched() -> None:
    t = Tagged("a", size=1, tags={"app": "web"})
    assert t.tags == {"app": "web"}


def test_stack_tags_tolerate_a_computed_tags_field() -> None:
    """A provider-owned `tags` output has nothing to merge at config time.

    The stored UNSET reads as a Ref through ``__getattribute__``, so the merge
    must inspect the raw stored value — otherwise it misdiagnoses the field as
    an unmergeable handle and raises."""

    class Reported(Resource):
        class Meta:
            provider: ClassVar[str] = "test"

        size: int = immutable()
        tags: dict[str, str] = computed()

    with Stack("prod", region="us-east-1", tags={"env": "prod"}):
        t = Reported("a", size=1)
    # Still unset until apply: reading yields the Ref, not the stack's tags.
    assert isinstance(t.tags, Ref)


# -- region inheritance ----------------------------------------------------

from .conftest import Bucket, Notifier  # noqa: E402 - after the local resources above


def test_stack_region_inherited() -> None:
    with Stack("prod", region="us-east-1"):
        b = Bucket("a", bucket_name="x")
    assert b.region == "us-east-1"


def test_explicit_region_wins_over_stack() -> None:
    with Stack("prod", region="us-east-1"):
        b = Bucket("a", bucket_name="x", region="eu-north-1")
    assert b.region == "eu-north-1"


def test_region_ignored_when_no_region_field() -> None:
    with Stack("prod", region="us-east-1"):
        n = Notifier("n", target_arn="arn:x")  # no region field -> unaffected
    assert n.node_id == "prod:test.Notifier:n"


def test_nested_stack_region_inner_wins() -> None:
    with Stack("outer", region="eu-west-1"):  # noqa: SIM117 - testing nesting
        with Stack("inner", region="us-east-1"):
            b = Bucket("a", bucket_name="x")
    assert b.region == "us-east-1"


def test_stack_requires_region() -> None:
    # A required *value*, not a required keyword: `config=` is the other source.
    with pytest.raises(RegistryError, match="non-empty region"):
        Stack("prod")
    with pytest.raises(RegistryError, match="non-empty region"):
        Stack("prod", region="")


# -- name-prefix composition ----------------------------------------------


def test_name_prefix_composes_from_logical_name() -> None:
    with Stack("dev", region="us-east-1", name_prefix="atlantide"):
        b = Bucket("assets")  # bucket_name omitted
    assert b.bucket_name == "atlantide-assets-dev"


def test_explicit_name_wins_over_prefix() -> None:
    with Stack("dev", region="us-east-1", name_prefix="atlantide"):
        b = Bucket("assets", bucket_name="literal-name")
    assert b.bucket_name == "literal-name"


def test_omitted_name_without_prefix_still_required() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Bucket("assets")  # no name_prefix, no bucket_name -> required field missing


# -- environment config ----------------------------------------------------


def _env(**values: object) -> EnvView:
    return Config(envs={"dev": values}).env("dev")  # type: ignore[arg-type]


def test_config_supplies_the_region() -> None:
    """What lets `Stack(env.name, config=env)` drop the separate `region=`."""
    with Stack("dev", config=_env(region="eu-north-1")):
        t = Thing("a", size=1)
    assert t.stack == "dev"


def test_config_supplies_the_name_prefix() -> None:
    with Stack("dev", config=_env(region="us-east-1", name_prefix="atlantide")):
        b = Bucket("assets")
    assert b.bucket_name == "atlantide-assets-dev"


def test_config_supplies_the_tags() -> None:
    with Stack("dev", config=_env(region="us-east-1", tags={"env": "dev"})):
        t = Tagged("a", size=1)
    assert t.tags == {"env": "dev"}


def test_an_explicit_argument_wins_over_the_config() -> None:
    """Same rule the resource layer uses: an explicit value wins."""
    env = _env(region="eu-north-1", name_prefix="fromconfig", tags={"env": "dev", "who": "config"})
    with Stack(
        "dev", config=env, region="us-east-1", name_prefix="explicit", tags={"who": "stack"}
    ):
        b = Bucket("assets")
        t = Tagged("a", size=1)
    assert b.region == "us-east-1"
    assert b.bucket_name == "explicit-assets-dev"
    assert t.tags == {"env": "dev", "who": "stack"}


def test_a_resource_tag_still_wins_over_a_config_tag() -> None:
    """Three levels: config tags < the stack's own tags= < the resource's."""
    with Stack("dev", config=_env(region="us-east-1", tags={"env": "dev", "app": "fromconfig"})):
        t = Tagged("a", size=1, tags={"app": "web"})
    assert t.tags == {"env": "dev", "app": "web"}


def test_current_config_is_ambient_inside_the_stack() -> None:
    """So an L2 Component reads it without callers threading it through."""
    env = _env(region="us-east-1")
    assert current_config() is None
    with Stack("dev", config=env):
        assert current_config() is env
    assert current_config() is None


def test_nested_stacks_see_the_inner_config() -> None:
    outer, inner = _env(region="us-east-1"), _env(region="eu-west-1")
    with Stack("outer", config=outer):
        assert current_config() is outer
        with Stack("inner", config=inner):
            assert current_config() is inner
        assert current_config() is outer  # restored on exit


def test_a_stack_without_a_config_clears_an_outer_one() -> None:
    """Otherwise a component in the inner stack would read the outer
    environment's values."""
    with Stack("outer", config=_env(region="us-east-1")):  # noqa: SIM117 - testing nesting
        with Stack("inner", region="us-east-1"):
            assert current_config() is None


def test_a_wrong_typed_well_known_key_names_its_environment() -> None:
    """`region=5` is caught here, not as a pydantic error on whichever resource
    was declared first."""
    config = Config(schema={"region": var(int)}, envs={"dev": {"region": 5}})
    with pytest.raises(LanguageError, match="environment 'dev': 'region' must be str"):
        Stack("dev", config=config.env("dev"))
