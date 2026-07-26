"""Translation between a declared :class:`SgRule` and EC2's ``IpPermission``.

Pure data mapping in both directions, kept apart from the handlers because it is
the fiddliest thing in the module and the easiest to reason about in isolation:
what a rule *permits* versus what merely describes it decides whether a config
edit revokes and re-authorizes real firewall rules.
"""

from __future__ import annotations

from typing import Any

from atlantide.providers.aws.resources.networking import SgRule


def rule_to_aws(rule: SgRule) -> dict[str, Any]:
    """One :class:`SgRule` as an ``IpPermission``."""
    permission: dict[str, Any] = {"IpProtocol": rule.protocol}
    if rule.protocol != "-1":
        permission["FromPort"] = rule.from_port
        permission["ToPort"] = rule.to_port
    if rule.cidr_blocks:
        permission["IpRanges"] = [
            {"CidrIp": cidr, "Description": rule.description}
            if rule.description
            else {"CidrIp": cidr}
            for cidr in rule.cidr_blocks
        ]
    if rule.ipv6_cidr_blocks:
        permission["Ipv6Ranges"] = [{"CidrIpv6": cidr} for cidr in rule.ipv6_cidr_blocks]
    if rule.source_security_group_id:
        permission["UserIdGroupPairs"] = [{"GroupId": rule.source_security_group_id}]
    return permission


def rules_from_aws(permissions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Live ``IpPermission``s in the shape a declared rule stores.

    Comparable with what config declared, so refresh can flag a rule added or
    removed out of band. Sorted for a stable comparison — AWS returns no order.
    """
    rules = [
        {
            "protocol": str(p.get("IpProtocol", "")),
            "from_port": p.get("FromPort"),
            "to_port": p.get("ToPort"),
            "cidr_blocks": sorted(r["CidrIp"] for r in p.get("IpRanges", [])),
            "ipv6_cidr_blocks": sorted(r["CidrIpv6"] for r in p.get("Ipv6Ranges", [])),
            "source_security_group_id": next(
                (g["GroupId"] for g in p.get("UserIdGroupPairs", [])), None
            ),
            # Read back rather than blanked. AWS stores the description on each
            # range, so a hardcoded "" differs from any rule that declared one —
            # including the allow-all egress `SecurityGroup` supplies by default,
            # which made every untouched security group report drift forever.
            "description": _description(p),
        }
        for p in permissions
    ]
    return sorted(rules, key=lambda r: (r["protocol"], str(r["from_port"]), r["cidr_blocks"]))


def _description(permission: dict[str, Any]) -> str:
    """The description AWS holds for this permission's first described range.

    One string for the whole rule because that is how :class:`SgRule` declares it,
    while AWS attaches one per range; `rule_to_aws` writes the same text to every
    range, so reading any of them back recovers what was declared.
    """
    ranges = [*permission.get("IpRanges", []), *permission.get("Ipv6Ranges", [])]
    return next((r["Description"] for r in ranges if r.get("Description")), "")


def atomic_units(permissions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each ``IpPermission`` split into one-range units.

    EC2 coalesces live rules by ``(protocol, from, to)`` and merges every range
    into one permission, so two declared rules for the same port with different
    CIDRs read back as a single merged permission. Comparing whole permissions
    then matches nothing: already-present ranges are re-authorized
    (``InvalidPermission.Duplicate`` fails the update) and merged live rules are
    revoked wholesale. Units are the grain EC2 actually authorizes and revokes
    at, so the delta is computed over them.
    """
    units: list[dict[str, Any]] = []
    for permission in permissions:
        # Annotated because the literal key tuple would otherwise infer a
        # `Literal[...]` key type, which does not unpack into a `str`-keyed dict.
        base: dict[str, Any] = {
            k: permission[k] for k in ("IpProtocol", "FromPort", "ToPort") if k in permission
        }
        for entry in permission.get("IpRanges", []):
            units.append({**base, "IpRanges": [entry]})
        for entry in permission.get("Ipv6Ranges", []):
            units.append({**base, "Ipv6Ranges": [entry]})
        for entry in permission.get("UserIdGroupPairs", []):
            units.append({**base, "UserIdGroupPairs": [entry]})
        if not (
            permission.get("IpRanges")
            or permission.get("Ipv6Ranges")
            or permission.get("UserIdGroupPairs")
        ):
            units.append(dict(base))
    return units


def identity(permission: dict[str, Any]) -> tuple[Any, ...]:
    """What makes two rules the same rule, ignoring description.

    Description is metadata AWS attaches to a range rather than part of what the
    rule permits; treating it as identity would revoke and re-authorize a rule
    every time a comment changed.
    """
    return (
        permission.get("IpProtocol"),
        permission.get("FromPort"),
        permission.get("ToPort"),
        tuple(sorted(r.get("CidrIp", "") for r in permission.get("IpRanges", []))),
        tuple(sorted(r.get("CidrIpv6", "") for r in permission.get("Ipv6Ranges", []))),
        tuple(sorted(g.get("GroupId", "") for g in permission.get("UserIdGroupPairs", []))),
    )


def has_rule(permissions: list[dict[str, Any]], permission: dict[str, Any]) -> bool:
    return any(identity(p) == identity(permission) for p in permissions)
