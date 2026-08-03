"""Config/var/EnvView as plain Python — the type, without the interpreter.

The language-level behaviour (error locations, attribute access through the
interpreter, `--env` filtering) lives in ``tests/lang/test_config.py``; this file
pins the data model those tests rest on.
"""

from __future__ import annotations

import copy

import pytest

from atlantide.core import Config, EnvSchema, EnvView, LanguageError, var

# -- schema ------------------------------------------------------------------


def test_a_default_fills_in_where_an_environment_is_silent() -> None:
    config = Config(
        schema={"size": var(int, default=1)},
        envs={"dev": {}, "prod": {"size": 5}},
    )
    assert config.env("dev").size == 1
    assert config.env("prod").size == 5


def test_a_variable_with_no_default_is_required_of_every_environment() -> None:
    with pytest.raises(LanguageError, match="'prod' is missing required variable 'domain'"):
        Config(schema={"domain": var(str)}, envs={"dev": {"domain": "d"}, "prod": {}})


def test_an_unknown_variable_names_what_is_declared() -> None:
    with pytest.raises(LanguageError) as exc:
        Config(schema={"domain": var(str)}, envs={"dev": {"domian": "typo"}})
    assert "environment 'dev': unknown variable 'domian'" in str(exc.value)
    assert "domain" in str(exc.value)


def test_a_wrong_type_names_the_environment() -> None:
    with pytest.raises(LanguageError, match="environment 'dev': variable 'size' expects int"):
        Config(schema={"size": var(int, default=1)}, envs={"dev": {"size": "5"}})


def test_a_bool_is_not_an_int() -> None:
    """`isinstance(True, int)` is True, so an unguarded check would let
    `var(int)` accept a flag."""
    with pytest.raises(LanguageError, match="variable 'size' expects int"):
        Config(schema={"size": var(int, default=1)}, envs={"dev": {"size": True}})
    # ...and the reverse: an int is not a bool.
    with pytest.raises(LanguageError, match="variable 'waf' expects bool"):
        Config(schema={"waf": var(bool, default=False)}, envs={"dev": {"waf": 1}})


def test_none_is_only_legal_where_the_declaration_made_it_so() -> None:
    Config(schema={"cert": var(str, default=None)}, envs={"dev": {"cert": None}})
    with pytest.raises(LanguageError, match="variable 'size' expects int"):
        Config(schema={"size": var(int, default=1)}, envs={"dev": {"size": None}})


def test_a_parameterised_generic_is_refused_rather_than_half_checked() -> None:
    with pytest.raises(LanguageError, match="parameterised generics"):
        var(list[str])  # type: ignore[arg-type]


def test_a_default_is_checked_against_its_own_type() -> None:
    with pytest.raises(LanguageError, match="var\\(int\\) default"):
        var(int, default="1")


def test_a_schema_entry_must_be_a_var() -> None:
    with pytest.raises(LanguageError, match="schema entry 'size' must be a var"):
        Config(schema={"size": 1}, envs={"dev": {}})  # type: ignore[dict-item]


def test_a_float_variable_accepts_an_int() -> None:
    """Widening only: 1 is a legal float, but True still is not."""
    config = Config(schema={"ratio": var(float, default=0.5)}, envs={"dev": {"ratio": 1}})
    assert config.env("dev").ratio == 1
    with pytest.raises(LanguageError, match="variable 'ratio' expects float"):
        Config(schema={"ratio": var(float, default=0.5)}, envs={"dev": {"ratio": True}})


def test_an_environment_must_map_variables_to_values() -> None:
    with pytest.raises(LanguageError, match="'dev' must be a mapping"):
        Config(envs={"dev": ["region", "a"]})  # type: ignore[dict-item]


@pytest.mark.parametrize("name", ["_private", "not an identifier", "2fast"])
def test_a_variable_nobody_could_read_back_is_refused(name: str) -> None:
    """`env.<name>` is how a variable is read, and `EnvView.__getattr__` answers
    underscore names with AttributeError. Catch it where it was written."""
    with pytest.raises(LanguageError, match="must be a plain identifier"):
        Config(schema={name: var(str, default="x")}, envs={"dev": {}})


def test_a_var_reads_back_as_the_source_that_declared_it() -> None:
    """Rather than `default=<object object at 0x...>` in a traceback."""
    assert repr(var(str)) == "var(str)"
    assert repr(var(int, default=5)) == "var(int, default=5)"


# -- well-known keys ---------------------------------------------------------


def test_region_tags_and_name_prefix_need_no_declaration() -> None:
    """What lets `Stack(env.name, config=env)` drop the separate `region=`."""
    config = Config(envs={"dev": {"region": "eu-north-1", "tags": {"env": "dev"}}})
    env = config.env("dev")
    assert env.region == "eu-north-1"
    assert env.tags == {"env": "dev"}
    assert env.name_prefix is None


def test_a_schema_may_re_declare_a_well_known_key_to_require_it() -> None:
    with pytest.raises(LanguageError, match="missing required variable 'region'"):
        Config(schema={"region": var(str)}, envs={"dev": {}})


# -- environments ------------------------------------------------------------


def test_environments_are_sorted_regardless_of_declaration_order() -> None:
    config = Config(envs={"prod": {"region": "a"}, "dev": {"region": "b"}})
    assert config.names() == ["dev", "prod"]
    assert [e.name for e in config.envs()] == ["dev", "prod"]


def test_an_environment_name_must_be_a_legal_stack_name() -> None:
    """It becomes one, so failing here names the cause and not the symptom."""
    with pytest.raises(Exception, match="invalid environment name"):
        Config(envs={"dev/1": {"region": "a"}})


def test_a_config_with_no_environments_is_refused() -> None:
    with pytest.raises(LanguageError, match="at least one environment"):
        Config(envs={})


def test_env_names_an_unknown_environment() -> None:
    config = Config(envs={"dev": {"region": "a"}})
    with pytest.raises(LanguageError, match="unknown environment 'prod'"):
        config.env("prod")


# -- EnvView -----------------------------------------------------------------


def test_an_unknown_attribute_names_the_environment_and_what_it_declares() -> None:
    env = Config(schema={"domain": var(str)}, envs={"dev": {"domain": "d"}}).env("dev")
    with pytest.raises(LanguageError) as exc:
        _ = env.doman
    assert "environment 'dev' has no variable 'doman'" in str(exc.value)
    assert "domain" in str(exc.value)


def test_subscript_reads_the_same_values_as_attribute_access() -> None:
    env = Config(schema={"domain": var(str)}, envs={"dev": {"domain": "d"}}).env("dev")
    assert env["domain"] == env.domain == "d"
    assert "domain" in env


def test_as_dict_is_plain_sorted_data() -> None:
    """A bare EnvView in a resource field fails IR canonicalization; this is the
    conversion that does not."""
    env = Config(schema={"b": var(int, default=2), "a": var(int, default=1)}, envs={"dev": {}}).env(
        "dev"
    )
    assert list(env.as_dict()) == sorted(env.as_dict())
    assert env.as_dict()["a"] == 1


def test_an_envview_carries_no_instance_dict() -> None:
    """__slots__, so the only reachable names are the declared variables."""
    env = Config(envs={"dev": {"region": "a"}}).env("dev")
    assert not hasattr(env, "__dict__")
    assert isinstance(env, EnvView)


def test_a_dunder_miss_is_an_attributeerror_not_a_languageerror() -> None:
    """Python probes dunders to discover protocols (copy, pickle, rich) and
    expects a miss to be an AttributeError; a LanguageError would crash the
    probe. Config authors cannot reach a dunder — validate.py rejects it."""
    env = Config(envs={"dev": {"region": "a"}}).env("dev")
    with pytest.raises(AttributeError):
        _ = env.__deepcopy__
    assert copy.deepcopy(env).region == "a"


# -- EnvSchema, as ordinary Python -------------------------------------------
#
# The interpreter builds these classes with `type()`, but `__init_subclass__`
# does the work either way — so the semantics are pinned here, without the
# interpreter in the picture.


class AppEnv(EnvSchema):
    region: str
    price_class: str = "PriceClass_100"
    versioning: bool = False
    cert: str | None = None


def test_a_declared_class_extracts_its_fields() -> None:
    assert AppEnv.__atlas_fields__ == {
        "region": var(str),
        "price_class": var(str, default="PriceClass_100"),
        "versioning": var(bool, default=False),
        "cert": var(str, default=None),
    }


def test_a_declared_default_does_not_shadow_an_environments_value() -> None:
    """`price_class: str = "..."` leaves a *class* attribute in ordinary Python,
    which normal lookup finds before `__getattr__` runs, so every environment
    would read the default instead of its own value."""
    config = Config(
        AppEnv,
        envs={"dev": {"region": "r"}, "prod": {"region": "r", "price_class": "All"}},
    )
    assert config.env("dev").price_class == "PriceClass_100"
    assert config.env("prod").price_class == "All"
    assert not hasattr(AppEnv, "price_class")


def test_a_declared_class_validates_exactly_like_a_var_schema() -> None:
    with pytest.raises(LanguageError, match="'dev' is missing required variable 'region'"):
        Config(AppEnv, envs={"dev": {}})
    with pytest.raises(LanguageError, match="variable 'region' expects str"):
        Config(AppEnv, envs={"dev": {"region": 5}})
    with pytest.raises(LanguageError, match="unknown variable 'nope'"):
        Config(AppEnv, envs={"dev": {"region": "r", "nope": 1}})


def test_a_typo_on_a_declared_class_names_the_environment() -> None:
    """What a generated dataclass would have answered with a bare AttributeError."""
    env = Config(AppEnv, envs={"dev": {"region": "r"}}).env("dev")
    with pytest.raises(LanguageError) as exc:
        _ = env.pirce_class
    assert "environment 'dev' has no variable 'pirce_class'" in str(exc.value)
    assert "price_class" in str(exc.value)


def test_a_declared_class_still_carries_the_well_known_keys() -> None:
    env = Config(AppEnv, envs={"dev": {"region": "r", "tags": {"env": "dev"}}}).env("dev")
    assert env.region == "r"
    assert env.tags == {"env": "dev"}
    assert env.name_prefix is None


@pytest.mark.parametrize("field", ["name", "get", "as_dict"])
def test_a_field_colliding_with_the_env_api_is_refused(field: str) -> None:
    with pytest.raises(LanguageError, match="collides"):
        type(AppEnv)("Bad", (EnvSchema,), {"__annotations__": {field: "str"}})


def test_a_field_type_outside_the_supported_set_is_refused() -> None:
    with pytest.raises(LanguageError, match="must be one of"):
        type(AppEnv)("Bad", (EnvSchema,), {"__annotations__": {"x": "set"}})


def test_a_parameterised_generic_field_is_refused() -> None:
    with pytest.raises(LanguageError, match="parameterised generics"):
        type(AppEnv)("Bad", (EnvSchema,), {"__annotations__": {"x": "list[str]"}})


def test_a_union_of_two_real_types_is_refused() -> None:
    with pytest.raises(LanguageError, match="only `X \\| None`"):
        type(AppEnv)("Bad", (EnvSchema,), {"__annotations__": {"x": "str | int"}})


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
