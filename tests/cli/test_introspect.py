"""Resource-type introspection powering the ``resources``/``schema`` commands."""

from __future__ import annotations

from atlantide.cli.commands.introspect import all_types, schema_rows
from atlantide.core.fields import Mutability
from atlantide.providers.aws import S3Bucket
from tests.support import Cli

cli = Cli()


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


def test_resources_lists_types() -> None:
    result = cli.run("resources")
    assert "aws.S3Bucket" in result.output
    assert "local.File" in result.output


def test_schema_shows_fields() -> None:
    result = cli.run("schema", "aws.S3Bucket")
    assert "bucket" in result.output
    assert "immutable" in result.output
    assert "computed" in result.output


def test_schema_unknown_type_suggests_available() -> None:
    result = cli.run("schema", "aws.Nope")
    assert result.exit_code == 1
    assert "unknown type" in result.output
    assert "aws.S3Bucket" in result.output  # suggestion list
