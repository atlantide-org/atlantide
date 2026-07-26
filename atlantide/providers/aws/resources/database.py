"""Database resources: DynamoDB table."""

from __future__ import annotations

from pydantic import model_validator

from atlantide.core import computed, immutable, mutable
from atlantide.providers.aws import validate as v
from atlantide.providers.aws.resources.base import RegionalResource, TaggedResource

_BILLING_MODE = v.one_of(("PAY_PER_REQUEST", "PROVISIONED"), "billing_mode")


class DynamoDbTable(RegionalResource, TaggedResource):
    """A DynamoDB table with a string hash key and optional string range key.

    ``table_name``, ``hash_key``, ``range_key``, their types and ``region`` are
    immutable; ``billing_mode``, ``ttl_attribute``, ``point_in_time_recovery`` and
    ``tags`` update in place. ``arn`` is computed.
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
    #: ``S`` string, ``N`` number, ``B`` binary. Was hardcoded to ``S``, which
    #: silently made a numeric key a string and broke every range query on it.
    hash_key_type: str = immutable(default="S")
    range_key_type: str = immutable(default="S")
    billing_mode: str = mutable(default="PAY_PER_REQUEST")
    #: Attribute holding an expiry timestamp; TTL is off when unset.
    ttl_attribute: str | None = mutable(default=None)
    #: Continuous backups. Off by default, as AWS has it.
    point_in_time_recovery: bool = mutable(default=False)
    arn: str = computed()

    @model_validator(mode="after")
    def _validate(self) -> DynamoDbTable:
        v.check(self.billing_mode, _BILLING_MODE)
        for name, value in (
            ("hash_key_type", self.hash_key_type),
            ("range_key_type", self.range_key_type),
        ):
            if value not in ("S", "N", "B"):
                raise ValueError(f"{name} must be S, N or B, got {value!r}")
        return self
