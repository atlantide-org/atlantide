"""Handlers for the read-only AWS lookups.

A data source's create and update are both the same read, and its delete does
nothing — atlantide did not make the thing and must not remove it. Expressed as
an ordinary :class:`AwsHandler` so the dispatcher, the executor and the diff need
no special case beyond the ``kind`` flag on the IR node.
"""

from __future__ import annotations

from typing import Any

from typing_extensions import override

from atlantide.providers.aws.handlers.base import AwsHandler
from atlantide.providers.aws.resources.data import (
    AwsAvailabilityZones,
    AwsCallerIdentity,
)


class _ReadOnlyHandler(AwsHandler[Any]):
    """CRUD for something that already exists: read, read, read, nothing."""

    @override
    def create(self, client: Any, res: Any) -> dict[str, Any]:
        return self._lookup(client, res)

    @override
    def update(self, client: Any, prior: dict[str, Any], res: Any) -> dict[str, Any]:
        # Reached when the query itself changed, so the answer is re-read.
        return self._lookup(client, res)

    @override
    def read(self, client: Any, res: Any) -> dict[str, Any] | None:
        return self._lookup(client, res)

    @override
    def delete(self, client: Any, res: Any) -> None:
        """Nothing. Deleting a lookup would delete infrastructure this config
        only ever read — the one thing a data source must never do."""

    def _lookup(self, client: Any, res: Any) -> dict[str, Any]:
        raise NotImplementedError


class AwsCallerIdentityHandler(_ReadOnlyHandler):
    service = "sts"
    resource_type = AwsCallerIdentity

    @override
    def _lookup(self, client: Any, res: AwsCallerIdentity) -> dict[str, Any]:
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
    def _lookup(self, client: Any, res: AwsAvailabilityZones) -> dict[str, Any]:
        response = client.describe_availability_zones(
            Filters=[{"Name": "state", "Values": [res.state]}]
        )
        # Sorted so two runs against one account produce the same list: the API
        # does not promise an order, and a config that indexes into it would
        # otherwise put a subnet in a different zone on a whim.
        zones = sorted(response.get("AvailabilityZones", []), key=lambda z: z["ZoneName"])
        return {
            "names": [zone["ZoneName"] for zone in zones],
            "zone_ids": [zone["ZoneId"] for zone in zones],
        }
