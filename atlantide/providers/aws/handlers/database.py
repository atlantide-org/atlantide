"""DynamoDB handler: tables."""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError
from typing_extensions import override

from atlantide.providers.aws.handlers.base import (
    AwsHandler,
    create_or_adopt,
    ignore_missing,
    sync_tags,
    tag_list,
    tags_from_list,
)
from atlantide.providers.aws.resources import DynamoDbTable


class DynamoDbTableHandler(AwsHandler[DynamoDbTable]):
    service = "dynamodb"
    resource_type = DynamoDbTable

    @override
    def create(self, client: Any, res: DynamoDbTable) -> dict[str, Any]:
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

        # See the IAM handler: adoption returns the create shape, not the
        # wider read shape.
        outputs = create_or_adopt(make, lambda: self._outputs(client, res))
        # A fresh table is CREATING for a while, and both follow-up calls reject
        # a table that is not ACTIVE (ResourceInUseException on real AWS). Cheap
        # when already ACTIVE: the waiter's first describe returns immediately.
        client.get_waiter("table_exists").wait(
            TableName=res.table_name, WaiterConfig={"Delay": 2, "MaxAttempts": 60}
        )
        _set_ttl(client, res)
        _set_pitr(client, res)
        return outputs

    def _outputs(self, client: Any, res: DynamoDbTable) -> dict[str, Any] | None:
        """Just what a create returns: the arn, or None if the table is absent."""
        try:
            return {"arn": client.describe_table(TableName=res.table_name)["Table"]["TableArn"]}
        except client.exceptions.ResourceNotFoundException:
            return None

    @override
    def read(self, client: Any, res: DynamoDbTable) -> dict[str, Any] | None:
        try:
            table = client.describe_table(TableName=res.table_name)["Table"]
        except client.exceptions.ResourceNotFoundException:
            return None
        arn = table["TableArn"]
        billing = table.get("BillingModeSummary", {}).get("BillingMode")
        return {
            "arn": arn,
            # A table with no summary is provisioned; AWS omits the field rather
            # than reporting the default.
            "billing_mode": billing or "PROVISIONED",
            "tags": tags_from_list(client.list_tags_of_resource(ResourceArn=arn).get("Tags", [])),
        }

    @override
    def update(self, client: Any, prior: dict[str, Any], res: DynamoDbTable) -> dict[str, Any]:
        arn = client.describe_table(TableName=res.table_name)["Table"]["TableArn"]
        _set_billing_mode(client, res)
        _set_ttl(client, res)
        _set_pitr(client, res)
        sync_tags(
            res.tags,
            live=lambda: tags_from_list(
                client.list_tags_of_resource(ResourceArn=arn).get("Tags", [])
            ),
            untag=lambda stale, _: client.untag_resource(ResourceArn=arn, TagKeys=stale),
            tag=lambda tags: client.tag_resource(ResourceArn=arn, Tags=tag_list(tags)),
        )
        return {"arn": arn}

    @override
    def delete(self, client: Any, res: DynamoDbTable) -> None:
        with ignore_missing():
            client.delete_table(TableName=res.table_name)


#: DynamoDB's way of saying "that is already the billing mode". Not an error —
#: an update that changes only tags legitimately produces it.
_NO_CHANGE = "no updates are to be performed"


def _set_billing_mode(client: Any, res: DynamoDbTable) -> None:
    """Apply the billing mode, tolerating only the genuine no-op.

    This used to swallow every ``ClientError``, which meant an ``AccessDenied``
    or a throttle read as success: the apply reported the table updated, state
    recorded the new billing mode, and the table kept the old one — with nothing
    anywhere to say so. Only the no-change message is expected here; anything
    else is a failure and belongs in front of the operator.
    """
    try:
        client.update_table(TableName=res.table_name, BillingMode=res.billing_mode)
    except ClientError as exc:
        message = exc.response.get("Error", {}).get("Message", "")
        if _NO_CHANGE not in message.lower():
            raise


def _table_schema(res: DynamoDbTable) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    attributes = [{"AttributeName": res.hash_key, "AttributeType": res.hash_key_type}]
    key_schema = [{"AttributeName": res.hash_key, "KeyType": "HASH"}]
    if res.range_key is not None:
        attributes.append({"AttributeName": res.range_key, "AttributeType": res.range_key_type})
        key_schema.append({"AttributeName": res.range_key, "KeyType": "RANGE"})
    return attributes, key_schema


def _set_ttl(client: Any, res: DynamoDbTable) -> None:
    """Turn TTL on or off. Sending the same state twice is an error, so the live
    setting is read first."""
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


def _set_pitr(client: Any, res: DynamoDbTable) -> None:
    """Continuous backups on or off.

    Failures surface: the call is idempotent (setting the current value
    succeeds), so any error here — AccessDenied, throttling, table not ACTIVE —
    means the backups are *not* in the declared state, and swallowing it would
    record PITR as enabled while it never was. This is the same bug the
    ``_set_billing_mode`` docstring records being fixed there.
    """
    client.update_continuous_backups(
        TableName=res.table_name,
        PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": res.point_in_time_recovery},
    )
