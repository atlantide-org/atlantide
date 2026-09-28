"""DynamoDB tables: CRUD, billing-mode updates, and key types."""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError

from atlantide.core import Context
from atlantide.core.errors import ProviderError
from atlantide.providers.aws import AwsProvider, DynamoDbTable
from tests.support import TEST_REGION

pytestmark = pytest.mark.usefixtures("aws_env")


async def test_dynamodb_table_crud() -> None:
    provider = AwsProvider()
    res = DynamoDbTable(
        "d", table_name="items", hash_key="pk", range_key="sk", tags={"team": "data"}
    )
    out = await provider.create(Context(), res)
    assert out["arn"].endswith(":table/items")
    schema = boto3.client("dynamodb").describe_table(TableName="items")["Table"]["KeySchema"]
    assert {k["KeyType"] for k in schema} == {"HASH", "RANGE"}

    await provider.update(Context(), out, res)
    await provider.delete(Context(), res)
    assert await provider.read(Context(), res) is None


async def test_a_denied_dynamodb_update_is_reported_not_swallowed() -> None:
    """A failed `update_table` raises instead of being suppressed.

    Guards against AccessDenied or throttling reading as success: the apply
    would report the table updated and state would record the new billing mode
    while the table kept the old one. Only the "nothing to change" response is
    tolerated.
    """
    provider = AwsProvider()
    res = DynamoDbTable("t", table_name="denied", hash_key="id")
    out = await provider.create(Context(), res)

    # The client the dispatcher hands the handler, not a look-alike from a
    # different (alias, service, region) cache entry.
    _handler, client = provider._dispatch(res, "update")

    def denied(**_kw: Any) -> None:
        raise ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}},
            "UpdateTable",
        )

    client.update_table = denied
    with pytest.raises(ProviderError):
        await provider.update(Context(), out, res)


async def test_an_unchanged_dynamodb_billing_mode_is_still_a_no_op() -> None:
    """An update touching only tags gets a "nothing to change" response, which
    is tolerated."""
    provider = AwsProvider()
    res = DynamoDbTable("t", table_name="unchanged", hash_key="id", tags={"a": "1"})
    out = await provider.create(Context(), res)

    updated = await provider.update(
        Context(), out, DynamoDbTable("t", table_name="unchanged", hash_key="id", tags={"a": "2"})
    )
    assert updated["arn"] == out["arn"]


# -- key types -------------------------------------------------------------------


async def test_a_numeric_range_key_is_created_as_a_number() -> None:
    """Guards against a numeric key being created as a string, which breaks every
    range query on it."""
    from atlantide.providers.aws import DynamoDbTable

    provider = AwsProvider()
    await provider.create(
        Context(),
        DynamoDbTable(
            "t",
            table_name="events",
            hash_key="pk",
            range_key="ts",
            range_key_type="N",
            region=TEST_REGION,
        ),
    )

    described = boto3.client("dynamodb", region_name=TEST_REGION).describe_table(
        TableName="events"
    )["Table"]
    types = {a["AttributeName"]: a["AttributeType"] for a in described["AttributeDefinitions"]}
    assert types["ts"] == "N"


def test_a_key_type_outside_the_three_is_refused() -> None:
    from atlantide.providers.aws import DynamoDbTable

    with pytest.raises(ValueError, match="must be S, N or B"):
        DynamoDbTable(
            "t",
            table_name="x",
            hash_key="pk",
            hash_key_type="STRING",
            region=TEST_REGION,
        )
