"""Database resources: DynamoDB table."""

from __future__ import annotations

from pydantic import model_validator

from atlantide.core import computed, immutable, mutable
from atlantide.providers.aws import validate as v
from atlantide.providers.aws.resources.base import AwsResource

_BILLING_MODE = v.one_of(("PAY_PER_REQUEST", "PROVISIONED"), "billing_mode")


class DynamoDbTable(AwsResource):
    """A DynamoDB table with a string hash key and optional string range key.

    ``table_name``, ``hash_key``, ``range_key`` and ``region`` are immutable;
    ``billing_mode`` and ``tags`` update in place. ``arn`` is computed.
    """

    class Action:
        """IAM action constants, e.g. ``allow(DynamoDbTable.Action.GetItem, on=...)``."""

        GetItem = "dynamodb:GetItem"
        PutItem = "dynamodb:PutItem"
        DeleteItem = "dynamodb:DeleteItem"
        Query = "dynamodb:Query"
        Scan = "dynamodb:Scan"

    table_name: str = immutable(physical_name=True)
    hash_key: str = immutable()
    range_key: str | None = immutable(default=None)
    billing_mode: str = mutable(default="PAY_PER_REQUEST")
    region: str = immutable()  # required (from the stack region)
    tags: dict[str, str] = mutable(default_factory=dict)
    arn: str = computed()

    @model_validator(mode="after")
    def _validate(self) -> DynamoDbTable:
        v.check(self.billing_mode, _BILLING_MODE)
        return self
