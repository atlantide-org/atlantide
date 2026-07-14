"""Field mutability metadata tests."""

from __future__ import annotations

from atlantide.core import Mutability, field_mutability, is_sensitive, physical_name_field

from .conftest import Bucket, Notifier


def test_mutability_map() -> None:
    assert field_mutability(Bucket) == {
        "bucket_name": Mutability.IMMUTABLE,
        "region": Mutability.IMMUTABLE,
        "versioning": Mutability.MUTABLE,
        "tags": Mutability.MUTABLE,
        "token": Mutability.MUTABLE,
        "arn": Mutability.COMPUTED,
    }


def test_defaults_apply() -> None:
    b = Bucket("logs", bucket_name="my-logs")
    assert b.region == "eu-west-1"
    assert b.versioning is False
    assert b.tags == {}


def test_sensitive_flag() -> None:
    assert is_sensitive(Bucket, "token") is True
    assert is_sensitive(Bucket, "bucket_name") is False


def test_physical_name_field() -> None:
    assert physical_name_field(Bucket) == "bucket_name"
    assert physical_name_field(Notifier) is None
