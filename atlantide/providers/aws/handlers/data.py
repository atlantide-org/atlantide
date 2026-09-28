"""Handlers for read-only AWS lookups (data sources).

They subclass :class:`AwsHandler`, so the dispatcher, the executor and the diff
need no special case beyond the ``kind`` flag on the IR node.
"""

from __future__ import annotations

from typing import Any, override

from atlantide.providers.aws.handlers.base import AwsHandler, Client
from atlantide.providers.aws.resources.data import (
    AwsAvailabilityZones,
    AwsCallerIdentity,
)


class _ReadOnlyHandler(AwsHandler[Any]):
    """CRUD for an existing object: create, read and update look it up; delete is a no-op."""

    @override
    def create(self, client: Client, res: Any) -> dict[str, Any]:
        return self._lookup(client, res)

    @override
    def update(self, client: Client, prior: dict[str, Any], res: Any) -> dict[str, Any]:
        # Runs when the query changes, so the answer is re-read.
        return self._lookup(client, res)

    @override
    def read(self, client: Client, res: Any) -> dict[str, Any] | None:
        return self._lookup(client, res)

    @override
    def delete(self, client: Client, res: Any) -> None:
        """No-op: deleting a lookup would delete infrastructure this config only read."""

    def _lookup(self, client: Client, res: Any) -> dict[str, Any]:
        raise NotImplementedError


class AwsCallerIdentityHandler(_ReadOnlyHandler):
    service = "sts"
    resource_type = AwsCallerIdentity

    @override
    def _lookup(self, client: Client, res: AwsCallerIdentity) -> dict[str, Any]:
        identity = client.get_caller_identity()
        return {
            "account_id": identity["Account"],
            "arn": identity["Arn"],
            "user_id": identity["UserId"],
        }


class AwsAvailabilityZonesHandler(_ReadOnlyHandler):
    service = "ec2"
    resource_type = AwsAvailabilityZones

    @override
    def _lookup(self, client: Client, res: AwsAvailabilityZones) -> dict[str, Any]:
        response = client.describe_availability_zones(
            Filters=[{"Name": "state", "Values": [res.state]}]
        )
        # The API does not guarantee order; sorting keeps list indices stable across runs.
        zones = sorted(response.get("AvailabilityZones", []), key=lambda z: z["ZoneName"])
        return {
            "names": [zone["ZoneName"] for zone in zones],
            "zone_ids": [zone["ZoneId"] for zone in zones],
        }
