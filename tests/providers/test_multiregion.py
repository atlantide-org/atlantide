"""Multi-region (region() sub-scope) and multi-account (provider_alias) support."""

from __future__ import annotations

import boto3
import pytest

from atlantide.core import Context, Stack, region
from atlantide.core.errors import ProviderError
from atlantide.providers.aws import AwsAlias, AwsProvider, S3Bucket
from atlantide.providers.aws.handlers import HANDLERS
from tests.support import aws_fixture

aws_env = aws_fixture(region="us-east-1")


def test_region_subscope_overrides_stack_region() -> None:
    with Stack("s", region="eu-north-1"):
        default = S3Bucket("a", bucket="bucket-a")
        with region("us-east-1"):
            override = S3Bucket("b", bucket="bucket-b")
        after = S3Bucket("c", bucket="bucket-c")
    assert default.region == "eu-north-1"
    assert override.region == "us-east-1"  # inner scope wins
    assert after.region == "eu-north-1"  # restored on exit


def test_provider_alias_defaults_none_and_is_read_by_handler() -> None:
    with Stack("s", region="us-east-1"):
        plain = S3Bucket("a", bucket="bucket-a")
        aliased = S3Bucket("b", bucket="bucket-b", provider_alias="prod")
    handler = HANDLERS["aws.S3Bucket"]
    assert handler.alias(plain) is None
    assert handler.alias(aliased) == "prod"


async def test_aliased_resource_uses_its_own_session() -> None:
    provider = AwsProvider(aliases={"prod": AwsAlias(profile=None)})
    with Stack("s", region="us-east-1"):
        res = S3Bucket("b", bucket="prod-bucket", provider_alias="prod")
    await provider.create(Context(), res)
    names = {b["Name"] for b in boto3.client("s3").list_buckets()["Buckets"]}
    assert "prod-bucket" in names
    # The alias got a session of its own. Sessions are built on demand, so the
    # default one does not exist until something actually asks for it — and when
    # it does, it is a different object, not the alias's.
    assert "prod" in provider._sessions and None not in provider._sessions
    assert provider._session_for(None) is not provider._sessions["prod"]


async def test_unknown_alias_fails() -> None:
    provider = AwsProvider()
    with Stack("s", region="us-east-1"):
        res = S3Bucket("b", bucket="bucket-b", provider_alias="ghost")
    with pytest.raises(ProviderError, match="unknown provider_alias 'ghost'"):
        await provider.create(Context(), res)
