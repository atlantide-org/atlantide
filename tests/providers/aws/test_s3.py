"""S3 buckets and bucket policies: CRUD, safe defaults, and teardown."""

from __future__ import annotations

import json

import boto3
import pytest

from atlantide.core import Context
from atlantide.core.errors import ProviderError
from atlantide.providers.aws import AwsProvider, S3Bucket, S3BucketPolicy, allow
from tests.providers.aws.conftest import exists

pytestmark = pytest.mark.usefixtures("aws_env")


async def test_create_bucket_and_outputs() -> None:
    provider = AwsProvider()
    out = await provider.create(Context(), S3Bucket("b", bucket="my-logs"))
    assert out == {
        "name": "my-logs",
        "arn": "arn:aws:s3:::my-logs",
        "objects_arn": "arn:aws:s3:::my-logs/*",
        "bucket": "my-logs",
        "regional_domain_name": "my-logs.s3.us-east-1.amazonaws.com",
    }
    assert exists("my-logs")


async def test_versioning_and_tags() -> None:
    provider = AwsProvider()
    await provider.create(
        Context(),
        S3Bucket("b", bucket="ver", versioning=True, tags={"env": "prod"}),
    )
    client = boto3.client("s3")
    assert client.get_bucket_versioning(Bucket="ver")["Status"] == "Enabled"
    tags = {t["Key"]: t["Value"] for t in client.get_bucket_tagging(Bucket="ver")["TagSet"]}
    assert tags == {"env": "prod"}


async def test_update_tags() -> None:
    provider = AwsProvider()
    res = S3Bucket("b", bucket="upd", tags={"a": "1"})
    await provider.create(Context(), res)
    await provider.update(Context(), {}, S3Bucket("b", bucket="upd", tags={"a": "2", "b": "3"}))
    client = boto3.client("s3")
    tags = {t["Key"]: t["Value"] for t in client.get_bucket_tagging(Bucket="upd")["TagSet"]}
    assert tags == {"a": "2", "b": "3"}


async def test_create_regional_bucket() -> None:
    # region != us-east-1 needs a matching client + LocationConstraint.
    provider = AwsProvider()
    out = await provider.create(Context(), S3Bucket("b", bucket="eu-bucket", region="eu-north-1"))
    assert out["bucket"] == "eu-bucket"
    client = boto3.client("s3", region_name="eu-north-1")
    loc = client.get_bucket_location(Bucket="eu-bucket")["LocationConstraint"]
    assert loc == "eu-north-1"


async def test_create_is_idempotent_when_already_owned() -> None:
    provider = AwsProvider()
    res = S3Bucket("b", bucket="owned-twice")
    await provider.create(Context(), res)
    # a second create (e.g. resuming a partial apply) must not error
    out = await provider.create(Context(), res)
    assert out["bucket"] == "owned-twice"


async def test_read_missing_is_none() -> None:
    provider = AwsProvider()
    assert await provider.read(Context(), S3Bucket("b", bucket="ghost")) is None


async def test_delete_bucket() -> None:
    provider = AwsProvider()
    res = S3Bucket("b", bucket="gone")
    await provider.create(Context(), res)
    assert exists("gone")
    await provider.delete(Context(), res)
    assert not exists("gone")


async def test_s3_bucket_policy_crud() -> None:
    provider = AwsProvider()
    await provider.create(Context(), S3Bucket("b", bucket="policed"))
    res = S3BucketPolicy(
        "p",
        bucket="policed",
        statements=[allow("s3:GetObject", on="arn:aws:s3:::policed/*", principal="*")],
    )
    assert await provider.create(Context(), res) == {}
    assert await provider.read(Context(), res) is not None

    doc = json.loads(boto3.client("s3").get_bucket_policy(Bucket="policed")["Policy"])
    assert doc["Statement"][0]["Principal"] == "*"

    await provider.delete(Context(), res)
    assert await provider.read(Context(), res) is None


# -- bucket safety ---------------------------------------------------------------


async def test_destroying_a_bucket_with_objects_fails_without_force_destroy() -> None:
    """S3 refuses to delete a non-empty bucket, so without `force_destroy` the
    stack's teardown fails."""
    provider = AwsProvider()
    res = S3Bucket("b", bucket="has-stuff")
    await provider.create(Context(), res)
    boto3.client("s3").put_object(Bucket="has-stuff", Key="a.txt", Body=b"x")

    with pytest.raises(ProviderError, match="not empty"):
        await provider.delete(Context(), res)


async def test_force_destroy_empties_the_bucket_first() -> None:
    provider = AwsProvider()
    res = S3Bucket("b", bucket="disposable", force_destroy=True)
    await provider.create(Context(), res)
    client = boto3.client("s3")
    for index in range(5):
        client.put_object(Bucket="disposable", Key=f"k{index}.txt", Body=b"x")

    await provider.delete(Context(), res)

    assert await provider.read(Context(), res) is None


async def test_force_destroy_clears_versions_and_delete_markers() -> None:
    """On a versioned bucket, deleting objects only adds delete markers — the
    bucket is still not empty and `delete_bucket` still refuses."""
    provider = AwsProvider()
    res = S3Bucket("b", bucket="versioned-disposable", versioning=True, force_destroy=True)
    await provider.create(Context(), res)
    client = boto3.client("s3")
    client.put_object(Bucket="versioned-disposable", Key="k.txt", Body=b"one")
    client.put_object(Bucket="versioned-disposable", Key="k.txt", Body=b"two")
    client.delete_object(Bucket="versioned-disposable", Key="k.txt")  # a delete marker

    await provider.delete(Context(), res)

    assert await provider.read(Context(), res) is None


async def test_a_bucket_is_private_and_encrypted_unless_told_otherwise() -> None:
    """Buckets default to private and encrypted: a public or unencrypted bucket
    must be declared explicitly."""
    provider = AwsProvider()
    res = S3Bucket("b", bucket="safe-by-default")
    await provider.create(Context(), res)

    live = await provider.read(Context(), res)

    assert live is not None
    assert live["block_public_access"] is True
    assert live["encryption"] == "AES256"


async def test_the_safe_defaults_can_be_turned_off_explicitly() -> None:
    provider = AwsProvider()
    res = S3Bucket("b", bucket="deliberately-open", block_public_access=False, encryption=None)
    await provider.create(Context(), res)

    live = await provider.read(Context(), res)

    assert live is not None
    assert live["block_public_access"] is False
    assert live["encryption"] is None


async def test_bucket_drift_on_the_safety_settings_is_observable() -> None:
    """A bucket opened up in the console has to show as drift, which means the
    read must report these fields rather than only the arn."""
    provider = AwsProvider()
    res = S3Bucket("b", bucket="opened-later")
    await provider.create(Context(), res)
    boto3.client("s3").delete_public_access_block(Bucket="opened-later")

    live = await provider.read(Context(), res)

    assert live is not None
    assert live["block_public_access"] is False, "the change is visible to refresh"
