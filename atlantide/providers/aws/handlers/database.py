"""DynamoDB handler: tables."""

from __future__ import annotations

import contextlib
from typing import Any

from atlantide.providers.aws.handlers.base import AwsHandler, ignore_missing, tag_list
from atlantide.providers.aws.resources import DynamoDbTable


class DynamoDbTableHandler(AwsHandler[DynamoDbTable]):
    service = "dynamodb"
    resource_type = DynamoDbTable

    def create(self, client: Any, res: DynamoDbTable) -> dict[str, Any]:
        attributes, key_schema = _table_schema(res)
        resp = client.create_table(
            TableName=res.table_name,
            AttributeDefinitions=attributes,
            KeySchema=key_schema,
            BillingMode=res.billing_mode,
            Tags=tag_list(res.tags),
        )
        return {"arn": resp["TableDescription"]["TableArn"]}

    def read(self, client: Any, res: DynamoDbTable) -> dict[str, Any] | None:
        try:
            resp = client.describe_table(TableName=res.table_name)
        except client.exceptions.ResourceNotFoundException:
            return None
        return {"arn": resp["Table"]["TableArn"]}

    def update(self, client: Any, prior: dict[str, Any], res: DynamoDbTable) -> dict[str, Any]:
        arn = client.describe_table(TableName=res.table_name)["Table"]["TableArn"]
        with contextlib.suppress(client.exceptions.ClientError):  # no-op if unchanged
            client.update_table(TableName=res.table_name, BillingMode=res.billing_mode)
        if res.tags:
            client.tag_resource(ResourceArn=arn, Tags=tag_list(res.tags))
        return {"arn": arn}

    def delete(self, client: Any, res: DynamoDbTable) -> None:
        with ignore_missing():
            client.delete_table(TableName=res.table_name)


def _table_schema(res: DynamoDbTable) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    attributes = [{"AttributeName": res.hash_key, "AttributeType": "S"}]
    key_schema = [{"AttributeName": res.hash_key, "KeyType": "HASH"}]
    if res.range_key is not None:
        attributes.append({"AttributeName": res.range_key, "AttributeType": "S"})
        key_schema.append({"AttributeName": res.range_key, "KeyType": "RANGE"})
    return attributes, key_schema
