"""Route53 handlers: hosted zones and record sets.

A zone is located by ``zone_id``, restored from state. A record has no id: its
identity is ``(zone_id, record_name, record_type)``, so create and update both
UPSERT the desired set and delete removes the exact live set. Route53 returns names
lowercased, with a trailing dot and with special characters as ``\\ooo`` octal escapes
(``*`` as ``\\052``), so record names are compared through :func:`_normalise_name`.
"""

from __future__ import annotations

import re
from typing import Any, override

from atlantide.providers.aws.handlers.base import (
    AwsHandler,
    Client,
    create_or_adopt,
    ignore_missing,
    known_id,
)
from atlantide.providers.aws.handlers.faults import absent_ok, not_found
from atlantide.providers.aws.resources import Route53HostedZone, Route53Record


class Route53HostedZoneHandler(AwsHandler[Route53HostedZone]):
    service = "route53"
    resource_type = Route53HostedZone
    identity_field = "zone_id"

    @override
    def create(self, client: Client, res: Route53HostedZone) -> dict[str, Any]:
        def make() -> dict[str, Any]:
            # Route53 raises HostedZoneAlreadyExists for a repeated
            # CallerReference rather than returning the existing zone, so a retry
            # adopts the zone created under that reference. Several zones may
            # share a domain, so the first one by name is not necessarily ours.
            resp = client.create_hosted_zone(
                Name=res.domain,
                CallerReference=res.node_id,
                HostedZoneConfig={"Comment": res.comment},
            )
            return _zone_outputs(resp["HostedZone"]["Id"], resp["DelegationSet"]["NameServers"])

        return create_or_adopt(make, lambda: self._find_by_reference(client, res))

    @override
    def read(self, client: Client, res: Route53HostedZone) -> dict[str, Any] | None:
        zid = known_id(res, self.identity_field) or self._find(client, res.domain)
        if zid is None:
            return None
        got = absent_ok(lambda: client.get_hosted_zone(Id=zid))
        if got is None:
            return None
        return _zone_outputs(zid, got["DelegationSet"]["NameServers"])

    @override
    def update(
        self, client: Client, prior: dict[str, Any], res: Route53HostedZone
    ) -> dict[str, Any]:
        zid = prior.get(self.identity_field) or known_id(res, self.identity_field)
        if zid is None:
            raise not_found(res, "update")
        client.update_hosted_zone_comment(Id=zid, Comment=res.comment)
        got = client.get_hosted_zone(Id=zid)
        return _zone_outputs(zid, got["DelegationSet"]["NameServers"])

    @override
    def delete(self, client: Client, res: Route53HostedZone) -> None:
        zid = known_id(res, self.identity_field)
        if zid is None:
            return
        with ignore_missing():
            client.delete_hosted_zone(Id=zid)

    @staticmethod
    def _find_by_reference(client: Client, res: Route53HostedZone) -> dict[str, Any] | None:
        """The zone for ``res.domain`` created under ``CallerReference == node_id``.

        Runs only on a create conflict, like ``CloudFrontDistributionHandler``'s
        lookup of the same name.
        """
        resp = client.list_hosted_zones_by_name(DNSName=res.domain)
        target = res.domain.rstrip(".")
        for zone in resp.get("HostedZones", []):
            if zone["Name"].rstrip(".") != target:
                continue
            got = client.get_hosted_zone(Id=zone["Id"])
            if got["HostedZone"].get("CallerReference") == res.node_id:
                return _zone_outputs(zone["Id"], got["DelegationSet"]["NameServers"])
        return None

    @staticmethod
    def _find(client: Client, domain: str) -> str | None:
        resp = client.list_hosted_zones_by_name(DNSName=domain)
        target = domain.rstrip(".")
        for zone in resp.get("HostedZones", []):
            if zone["Name"].rstrip(".") == target:
                return str(zone["Id"].split("/")[-1])
        return None


class Route53RecordHandler(AwsHandler[Route53Record]):
    service = "route53"
    resource_type = Route53Record

    @override
    def create(self, client: Client, res: Route53Record) -> dict[str, Any]:
        client.change_resource_record_sets(
            HostedZoneId=res.zone_id, ChangeBatch=_batch("UPSERT", _record_set(res))
        )
        return {}

    @override
    def read(self, client: Client, res: Route53Record) -> dict[str, Any] | None:
        live = self._live_set(client, res)
        if live is None:
            return None
        # Report the record contents so refresh detects a repointed record.
        observed: dict[str, Any] = {
            "records": [r["Value"] for r in live.get("ResourceRecords", [])],
        }
        if "TTL" in live:
            observed["ttl"] = int(live["TTL"])
        if (alias := live.get("AliasTarget")) is not None:
            observed["alias"] = {
                "name": alias["DNSName"].rstrip("."),
                "zone_id": alias["HostedZoneId"],
                "evaluate_target_health": alias.get("EvaluateTargetHealth", False),
            }
        return observed

    @override
    def update(self, client: Client, prior: dict[str, Any], res: Route53Record) -> dict[str, Any]:
        return self.create(client, res)  # UPSERT overwrites the set in place

    @override
    def delete(self, client: Client, res: Route53Record) -> None:
        with ignore_missing():
            live = self._live_set(client, res)
            if live is not None:  # DELETE needs the exact live TTL and values
                client.change_resource_record_sets(
                    HostedZoneId=res.zone_id, ChangeBatch=_batch("DELETE", live)
                )

    @staticmethod
    def _live_set(client: Client, res: Route53Record) -> dict[str, Any] | None:
        resp = absent_ok(
            lambda: client.list_resource_record_sets(
                HostedZoneId=res.zone_id,
                StartRecordName=res.record_name,
                StartRecordType=res.record_type,
                MaxItems="1",
            )
        )
        if resp is None:
            return None
        target = _normalise_name(res.record_name)
        rrsets: list[dict[str, Any]] = resp.get("ResourceRecordSets", [])
        for rrset in rrsets:
            if _normalise_name(rrset["Name"]) == target and rrset["Type"] == res.record_type:
                return rrset
        return None


#: A Route53 octal escape, e.g. ``\052`` for ``*``.
_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")


def _normalise_name(name: str) -> str:
    """A record name in the form Route53 compares it: escapes decoded, lowercase,
    no trailing dot. ``*.Example.com`` and ``\\052.example.com.`` are the same."""
    decoded = _OCTAL_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), name)
    return decoded.lower().rstrip(".")


def _record_set(res: Route53Record) -> dict[str, Any]:
    """The ``ResourceRecordSet`` for this record.

    An alias set omits ``TTL`` and ``ResourceRecords``: Route 53 rejects an alias
    set that carries them, since an alias inherits the target's TTL.
    """
    if res.alias is not None:
        return {
            "Name": res.record_name,
            "Type": res.record_type,
            "AliasTarget": {
                "DNSName": res.alias.name,
                "HostedZoneId": res.alias.zone_id,
                "EvaluateTargetHealth": res.alias.evaluate_target_health,
            },
        }
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
