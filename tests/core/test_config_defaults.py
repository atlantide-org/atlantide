"""A declared ``EnvSchema`` field's default is held to exactly what ``var()`` accepts.

The two schema spellings promise identical validation (see ``core/config.py``).
Unchecked, ``size: int = 'x'`` would be stored as is, and every environment
relying on it would read a str where an int was declared.
The table below crosses every supported type with a spread of values, so a rule
that drifts on either side — bool vs int, int for float, ``None`` as "optional"
— fails here.
"""

from __future__ import annotations

from typing import Any

import pytest

from atlantide.core import Config, EnvSchema, LanguageError, var
from atlantide.core.config import SUPPORTED_FIELD_TYPE_NAMES

_TYPES: dict[str, type] = {"str": str, "int": int, "float": float, "bool": bool}
_TYPES |= {"list": list, "dict": dict}


def _fn() -> int:
    return 1


#: One value of every shape a default could take, including those with a
#: non-obvious `isinstance` answer (bool is an int; an int is a valid float).
_VALUES: list[Any] = [
    "x",
    "",
    0,
    7,
    1.5,
    True,
    False,
    [1],
    [],
    {"a": 1},
    {},
    None,
    (1,),
    _fn,
    lambda: 1,
]

_CASES = [(name, value) for name in sorted(_TYPES) for value in _VALUES]


def _outcome(build: Any) -> str | None:
    """``None`` when ``build()`` succeeds, else its ``LanguageError`` message."""
    try:
        build()
    except LanguageError as exc:
        return str(exc)
    return None


def _var_accepts(type_name: str, value: Any) -> bool:
    return _outcome(lambda: var(_TYPES[type_name], default=value)) is None


def _schema(annotation: Any, default: Any, *, interpreter_form: bool) -> type[EnvSchema]:
    """A one-field schema, built the way ordinary Python or the interpreter builds it."""
    namespace: dict[str, Any] = {"__slots__": (), "__annotations__": {"x": annotation}}
    if interpreter_form:
        namespace["__atlas_defaults__"] = {"x": default}
    else:
        namespace["x"] = default
    return type("S", (EnvSchema,), namespace)


def test_the_table_covers_every_supported_field_type() -> None:
    assert set(_TYPES) == SUPPORTED_FIELD_TYPE_NAMES


@pytest.mark.parametrize("interpreter_form", [False, True], ids=["python", "interpreter"])
@pytest.mark.parametrize("spelling", ["object", "string", "nullable-object", "nullable-string"])
@pytest.mark.parametrize(("type_name", "value"), _CASES)
def test_a_field_default_is_accepted_exactly_when_var_accepts_it(
    type_name: str, value: Any, spelling: str, interpreter_form: bool
) -> None:
    type_ = _TYPES[type_name]
    annotation: Any = {
        "object": type_,
        "string": type_name,
        "nullable-object": type_ | None,
        "nullable-string": f"{type_name} | None",
    }[spelling]

    expected = _outcome(lambda: var(type_, default=value))
    actual = _outcome(lambda: _schema(annotation, value, interpreter_form=interpreter_form))

    if expected is None:
        assert actual is None
    else:
        assert actual == expected.replace(f"var({type_name})", "S.x", 1)


@pytest.mark.parametrize(
    ("type_name", "value"),
    [(name, value) for name, value in _CASES if _var_accepts(name, value)],
)
def test_an_accepted_default_resolves_to_the_same_value_either_way(
    type_name: str, value: Any
) -> None:
    type_ = _TYPES[type_name]
    by_var = Config(schema={"x": var(type_, default=value)}, envs={"dev": {}})
    by_class = Config(_schema(type_, value, interpreter_form=False), envs={"dev": {}})
    assert by_class.env("dev")["x"] is by_var.env("dev")["x"] is value


@pytest.mark.parametrize(
    ("annotation", "default", "message"),
    [
        (int, "x", "AppEnv.size default 'x' is a str"),
        (int, True, "AppEnv.size default True is a bool"),
        (bool, 1, "AppEnv.size default 1 is a int"),
        (str, 1.5, "AppEnv.size default 1.5 is a float"),
        (list, {"a": 1}, "AppEnv.size default {'a': 1} is a dict"),
        (int | None, "x", "AppEnv.size default 'x' is a str"),
    ],
)
def test_a_wrong_default_names_the_schema_class_and_field(
    annotation: Any, default: Any, message: str
) -> None:
    with pytest.raises(LanguageError) as exc:
        type("AppEnv", (EnvSchema,), {"__annotations__": {"size": annotation}, "size": default})
    assert str(exc.value) == message


def test_a_function_default_renders_without_a_memory_address() -> None:
    with pytest.raises(LanguageError) as exc:

        class AppEnv(EnvSchema):
            retries: int = lambda: 1  # type: ignore[assignment]

    assert str(exc.value) == "AppEnv.retries default <lambda> is a function"
    assert "0x" not in str(exc.value)


def test_a_none_default_still_makes_a_field_optional_and_nullable() -> None:
    class AppEnv(EnvSchema):
        cert: str = None  # type: ignore[assignment]

    config = Config(AppEnv, envs={"dev": {}, "prod": {"cert": None}})
    assert config.env("dev").cert is None
    assert config.env("prod").cert is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
