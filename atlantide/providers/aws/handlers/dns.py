"""Route53 handlers: hosted zones and record sets.

A zone is id-located (``zone_id``, restored from state). A record has no id — its
identity is ``(zone_id, record_name, record_type)`` — so create/update both UPSERT
the desired set, and delete removes the exact live set. Route53 returns names with
a trailing dot, so every name comparison is normalised with ``rstrip(".")``.
"""

from __future__ import annotations

from typing import Any

from atlantide.core.errors import ProviderError
from atlantide.providers.aws.handlers.base import AwsHandler, ignore_missing, known_id
from atlantide.providers.aws.resources import Route53HostedZone, Route53Record


class Route53HostedZoneHandler(AwsHandler[Route53HostedZone]):
    service = "route53"
    resource_type = Route53HostedZone

    def create(self, client: Any, res: Route53HostedZone) -> dict[str, Any]:
        resp = client.create_hosted_zone(
            Name=res.domain,
            CallerReference=res.node_id,  # stable reference; a retried create is idempotent
            HostedZoneConfig={"Comment": res.comment},
        )
        return _zone_outputs(resp["HostedZone"]["Id"], resp["DelegationSet"]["NameServers"])

    def read(self, client: Any, res: Route53HostedZone) -> dict[str, Any] | None:
        zid = known_id(res, "zone_id") or self._find(client, res.domain)
        if zid is None:
            return None
        try:
            got = client.get_hosted_zone(Id=zid)
        except client.exceptions.ClientError:
            return None
        return _zone_outputs(zid, got["DelegationSet"]["NameServers"])

    def update(self, client: Any, prior: dict[str, Any], res: Route53HostedZone) -> dict[str, Any]:
        zid = prior.get("zone_id") or known_id(res, "zone_id")
        if zid is None:  # update only runs on an existing (already-created) zone
            raise ProviderError(
                "Route53HostedZone not found", op="update", resource_type=res.type_name()
            )
        client.update_hosted_zone_comment(Id=zid, Comment=res.comment)
        got = client.get_hosted_zone(Id=zid)
        return _zone_outputs(zid, got["DelegationSet"]["NameServers"])

    def delete(self, client: Any, res: Route53HostedZone) -> None:
        zid = known_id(res, "zone_id")
        if zid is None:
            return
        with ignore_missing():
            client.delete_hosted_zone(Id=zid)

    @staticmethod
    def _find(client: Any, domain: str) -> str | None:
        resp = client.list_hosted_zones_by_name(DNSName=domain)
        target = domain.rstrip(".")
        for zone in resp.get("HostedZones", []):
            if zone["Name"].rstrip(".") == target:
                return str(zone["Id"].split("/")[-1])
        return None


class Route53RecordHandler(AwsHandler[Route53Record]):
    service = "route53"
    resource_type = Route53Record

    def create(self, client: Any, res: Route53Record) -> dict[str, Any]:
        client.change_resource_record_sets(
            HostedZoneId=res.zone_id, ChangeBatch=_batch("UPSERT", _record_set(res))
        )
        return {}

    def read(self, client: Any, res: Route53Record) -> dict[str, Any] | None:
        return None if self._live_set(client, res) is None else {}

    def update(self, client: Any, prior: dict[str, Any], res: Route53Record) -> dict[str, Any]:
        return self.create(client, res)  # UPSERT overwrites the set in place

    def delete(self, client: Any, res: Route53Record) -> None:
        with ignore_missing():
            live = self._live_set(client, res)
            if live is not None:  # DELETE needs the exact live TTL + values
                client.change_resource_record_sets(
                    HostedZoneId=res.zone_id, ChangeBatch=_batch("DELETE", live)
                )

    @staticmethod
    def _live_set(client: Any, res: Route53Record) -> dict[str, Any] | None:
        try:
            resp = client.list_resource_record_sets(
                HostedZoneId=res.zone_id,
                StartRecordName=res.record_name,
                StartRecordType=res.record_type,
                MaxItems="1",
            )
        except client.exceptions.ClientError:
            return None
        target = res.record_name.rstrip(".")
        rrsets: list[dict[str, Any]] = resp.get("ResourceRecordSets", [])
        for rrset in rrsets:
            if rrset["Name"].rstrip(".") == target and rrset["Type"] == res.record_type:
                return rrset
        return None


def _record_set(res: Route53Record) -> dict[str, Any]:
    return {
        "Name": res.record_name,
        "Type": res.record_type,
        "TTL": res.ttl,
        "ResourceRecords": [{"Value": value} for value in res.records],
    }


def _batch(action: str, record_set: dict[str, Any]) -> dict[str, Any]:
    return {"Changes": [{"Action": action, "ResourceRecordSet": record_set}]}


def _zone_outputs(zone_id: str, name_servers: list[str]) -> dict[str, Any]:
    return {"zone_id": zone_id.split("/")[-1], "name_servers": name_servers}
