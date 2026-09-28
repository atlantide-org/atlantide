"""DynamoDB handler: tables."""

from __future__ import annotations

from typing import Any, override

from botocore.exceptions import ClientError

from atlantide.providers.aws.handlers.base import (
    AwsHandler,
    Client,
    create_or_adopt,
    ignore_missing,
    sync_tags,
    tag_list,
    tags_from_list,
)
from atlantide.providers.aws.resources import DynamoDbTable
from atlantide.util.aws import error_message


class DynamoDbTableHandler(AwsHandler[DynamoDbTable]):
    service = "dynamodb"
    resource_type = DynamoDbTable

    @override
    def create(self, client: Client, res: DynamoDbTable) -> dict[str, Any]:
        def make() -> dict[str, Any]:
            attributes, key_schema = _table_schema(res)
            resp = client.create_table(
                TableName=res.table_name,
                AttributeDefinitions=attributes,
                KeySchema=key_schema,
                BillingMode=res.billing_mode,
                Tags=tag_list(res.tags),
            )
            return {"arn": resp["TableDescription"]["TableArn"]}

        # Adopt to the create shape, not the read shape (as in the IAM role handler).
        outputs = create_or_adopt(make, lambda: self._outputs(client, res))
        # The TTL and PITR calls reject a table that is not ACTIVE
        # (ResourceInUseException). On an ACTIVE table the waiter returns after one describe.
        client.get_waiter("table_exists").wait(
            TableName=res.table_name, WaiterConfig={"Delay": 2, "MaxAttempts": 60}
        )
        _set_ttl(client, res)
        _set_pitr(client, res)
        return outputs

    def _outputs(self, client: Client, res: DynamoDbTable) -> dict[str, Any] | None:
        """The create-shaped outputs: the arn, or None if the table is absent."""
        try:
            return {"arn": client.describe_table(TableName=res.table_name)["Table"]["TableArn"]}
        except client.exceptions.ResourceNotFoundException:
            return None

    @override
    def read(self, client: Client, res: DynamoDbTable) -> dict[str, Any] | None:
        try:
            table = client.describe_table(TableName=res.table_name)["Table"]
        except client.exceptions.ResourceNotFoundException:
            return None
        arn = table["TableArn"]
        billing = table.get("BillingModeSummary", {}).get("BillingMode")
        return {
            "arn": arn,
            # AWS omits the summary for a provisioned table.
            "billing_mode": billing or "PROVISIONED",
            "tags": _live_tags(client, arn),
        }

    @override
    def update(self, client: Client, prior: dict[str, Any], res: DynamoDbTable) -> dict[str, Any]:
        arn = client.describe_table(TableName=res.table_name)["Table"]["TableArn"]
        _set_billing_mode(client, res)
        _set_ttl(client, res)
        _set_pitr(client, res)
        sync_tags(
            res.tags,
            live=lambda: _live_tags(client, arn),
            untag=lambda stale, _: client.untag_resource(ResourceArn=arn, TagKeys=stale),
            tag=lambda tags: client.tag_resource(ResourceArn=arn, Tags=tag_list(tags)),
        )
        return {"arn": arn}

    @override
    def delete(self, client: Client, res: DynamoDbTable) -> None:
        with ignore_missing():
            client.delete_table(TableName=res.table_name)
        # delete_table is asynchronous. A replace creates next, and a create against a
        # table still DELETING answers ResourceInUseException, which create_or_adopt
        # would take as "already exists" and adopt the dying table.
        client.get_waiter("table_not_exists").wait(
            TableName=res.table_name, WaiterConfig={"Delay": 2, "MaxAttempts": 60}
        )


#: DynamoDB's error message when the billing mode is already set. An update that
#: changes only tags produces it.
_NO_CHANGE = "no updates are to be performed"


def _set_billing_mode(client: Client, res: DynamoDbTable) -> None:
    """Apply the billing mode, ignoring only the no-change error.

    Any other ``ClientError`` (e.g. ``AccessDenied``, throttling) propagates, so
    state never records a billing mode the table does not have.
    """
    try:
        client.update_table(TableName=res.table_name, BillingMode=res.billing_mode)
    except ClientError as exc:
        if _NO_CHANGE not in error_message(exc).lower():
            raise


def _live_tags(client: Client, arn: str) -> dict[str, str]:
    return tags_from_list(client.list_tags_of_resource(ResourceArn=arn).get("Tags", []))


def _table_schema(res: DynamoDbTable) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    attributes = [{"AttributeName": res.hash_key, "AttributeType": res.hash_key_type}]
    key_schema = [{"AttributeName": res.hash_key, "KeyType": "HASH"}]
    if res.range_key is not None:
        attributes.append({"AttributeName": res.range_key, "AttributeType": res.range_key_type})
        key_schema.append({"AttributeName": res.range_key, "KeyType": "RANGE"})
    return attributes, key_schema


def _set_ttl(client: Client, res: DynamoDbTable) -> None:
    """Turn TTL on or off.

    ``update_time_to_live`` rejects a request that repeats the current state, so
    the live setting is read first.
    """
    live = client.describe_time_to_live(TableName=res.table_name)
    current = live.get("TimeToLiveDescription", {})
    enabled = current.get("TimeToLiveStatus") in ("ENABLED", "ENABLING")
    wanted = res.ttl_attribute is not None
    if enabled == wanted and current.get("AttributeName") == res.ttl_attribute:
        return
    client.update_time_to_live(
        TableName=res.table_name,
        TimeToLiveSpecification={
            "Enabled": wanted,
            "AttributeName": res.ttl_attribute or current.get("AttributeName", "ttl"),
        },
    )


def _set_pitr(client: Client, res: DynamoDbTable) -> None:
    """Turn continuous backups (PITR) on or off.

    The call is idempotent, so every error propagates: any failure means PITR is
    not in the declared state.
    """
    client.update_continuous_backups(
        TableName=res.table_name,
        PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": res.point_in_time_recovery},
    )
