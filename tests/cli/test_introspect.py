"""Resource-type introspection powering the ``resources``/``schema`` commands."""

from __future__ import annotations

from atlantide.cli.introspect import all_types, schema_rows
from atlantide.core.fields import Mutability
from atlantide.providers.aws import S3Bucket


def test_all_types_spans_providers() -> None:
    types = all_types()
    assert "local.File" in types
    assert "aws.S3Bucket" in types
    assert types["aws.S3Bucket"] is S3Bucket


def test_schema_rows_reflect_field_metadata() -> None:
    rows = {r.name: r for r in schema_rows(S3Bucket)}

    assert rows["bucket"].mutability is Mutability.IMMUTABLE
    assert rows["bucket"].required is True
    assert rows["bucket"].default == ""

    assert rows["versioning"].mutability is Mutability.MUTABLE
    assert rows["versioning"].required is False
    assert rows["versioning"].default == "False"

    assert rows["tags"].type.startswith("dict")
    assert rows["tags"].default == "{}"

    # computed outputs are never required inputs and carry no default
    assert rows["arn"].mutability is Mutability.COMPUTED
    assert rows["arn"].required is False
    assert rows["arn"].default == ""
