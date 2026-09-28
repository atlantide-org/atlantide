"""The validator's tables agree with what they describe.

The import rejection names the allowed surface in prose, and the ``EnvSchema``
check restates `atlantide.core.config`'s field rules by name; each is pinned to
its source here so the two cannot drift apart.
"""

from __future__ import annotations

import builtins

import pytest

import atlantide.lang.validate as validate
import atlantide.lang.validate.schema as schema
import atlantide.lang.validate.validator as validator
from atlantide.core import is_successful
from atlantide.core.config import (
    RESERVED_FIELD_NAMES,
    SUPPORTED_FIELD_TYPE_NAMES,
    Config,
    var,
)
from atlantide.core.errors import LanguageError
from atlantide.lang import evaluate_source, validate_source
from atlantide.lang.validate.imports import _ALLOWED_IMPORT_PREFIXES, ALLOWED_IMPORTS_DESC


def test_package_exports() -> None:
    assert set(validate.__all__) == {
        "DEFAULT_SURFACE",
        "ENV_SCHEMA_BASE",
        "FORBIDDEN_CORE_NAMES",
        "LanguageSurface",
        "attribute_rejection",
        "engine_import_message",
        "import_allowed",
        "private_import_message",
        "validate_source",
    }


# -- the import rejection names the whole surface ------------------------------


@pytest.mark.parametrize("prefix", _ALLOWED_IMPORT_PREFIXES)
def test_import_rejection_names_every_allowed_prefix(prefix: str) -> None:
    tail = prefix.removeprefix("atlantide")
    spellings = {f"'{prefix}'", f"'{tail}'", f"'{tail}.*'"}
    assert any(s in ALLOWED_IMPORTS_DESC for s in spellings), prefix


def test_import_rejection_text() -> None:
    error = validate_source("import os\n").failure()
    assert str(error).startswith(
        "import of 'os' is not allowed (only 'atlantide.core', '.policy', "
        "'.providers.*', '.components.*') — config must be a pure function"
    )


# -- EnvSchema field rules come from core.config --------------------------------


def test_schema_tables_are_core_configs() -> None:
    # The validator reads core.config's own sets, not copies of them.
    assert vars(schema)["SUPPORTED_FIELD_TYPE_NAMES"] is SUPPORTED_FIELD_TYPE_NAMES
    assert vars(validator)["RESERVED_FIELD_NAMES"] is RESERVED_FIELD_NAMES
    assert {"str", "int", "float", "bool", "list", "dict"} == SUPPORTED_FIELD_TYPE_NAMES
    assert {"name", "get", "as_dict"} == RESERVED_FIELD_NAMES


def _schema(field: str, annotation: str) -> str:
    return (
        "from atlantide.core import EnvSchema\n"
        f"class E(EnvSchema):\n    {field}: {annotation} | None = None\n"
    )


@pytest.mark.parametrize("type_name", sorted(SUPPORTED_FIELD_TYPE_NAMES))
def test_every_type_var_accepts_is_a_valid_annotation(type_name: str) -> None:
    var(getattr(builtins, type_name))
    assert is_successful(evaluate_source(_schema("x", type_name)))


@pytest.mark.parametrize("field", sorted(RESERVED_FIELD_NAMES))
def test_a_reserved_name_is_refused_by_both_checks(field: str) -> None:
    assert "collides" in str(validate_source(_schema(field, "str")).failure())
    with pytest.raises(LanguageError, match="collides"):
        Config(schema={field: var(str)}, envs={"dev": {}})


def test_reserved_field_message_lists_names_sorted() -> None:
    error = validate_source(_schema("get", "str")).failure()
    assert "(as_dict, get, name)" in str(error)


def test_field_type_message_lists_types_sorted() -> None:
    error = validate_source(_schema("x", "set")).failure()
    assert "must be one of bool, dict, float, int, list, str" in str(error)
