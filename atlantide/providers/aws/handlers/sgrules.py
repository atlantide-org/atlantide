"""Translation between a declared :class:`SgRule` and EC2's ``IpPermission``.

Pure data mapping in both directions. The split between what a rule permits and
what only describes it decides whether a config edit revokes and re-authorizes
firewall rules.
"""

from __future__ import annotations

import json
from typing import Any

from atlantide.providers.aws.resources.networking import SgRule


def rule_to_aws(rule: SgRule) -> dict[str, Any]:
    """One :class:`SgRule` as an ``IpPermission``.

    AWS stores a description per range, so the rule's description is written to
    every range and group pair.
    """
    permission: dict[str, Any] = {"IpProtocol": rule.protocol}
    if rule.protocol != "-1":
        permission["FromPort"] = rule.from_port
        permission["ToPort"] = rule.to_port
    described = {"Description": rule.description} if rule.description else {}
    if rule.cidr_blocks:
        permission["IpRanges"] = [{"CidrIp": cidr, **described} for cidr in rule.cidr_blocks]
    if rule.ipv6_cidr_blocks:
        permission["Ipv6Ranges"] = [
            {"CidrIpv6": cidr, **described} for cidr in rule.ipv6_cidr_blocks
        ]
    if rule.source_security_group_id:
        permission["UserIdGroupPairs"] = [{"GroupId": rule.source_security_group_id, **described}]
    return permission


def rules_from_aws(permissions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``IpPermission``s as single-source rules in the shape a declared rule stores.

    Lets refresh flag a rule added or removed out of band. EC2 merges every range
    sharing ``(protocol, from, to)`` into one permission, so rules are split to one
    source each (see :func:`atomic_units`); sorted and deduplicated because AWS
    returns no stable order. Declared rules passed through :func:`rule_to_aws`
    normalize to the same form.
    """
    rules = {}
    for unit in atomic_units(permissions):
        rule = _unit_rule(unit)
        rules[json.dumps(rule, sort_keys=True)] = rule
    return [rules[key] for key in sorted(rules)]


def _unit_rule(unit: dict[str, Any]) -> dict[str, Any]:
    """One single-source ``IpPermission`` unit as a rule mapping."""
    source = _source(unit)
    return {
        "protocol": str(unit.get("IpProtocol", "")),
        "from_port": unit.get("FromPort"),
        "to_port": unit.get("ToPort"),
        "cidr_blocks": [source["CidrIp"]] if "CidrIp" in source else [],
        "ipv6_cidr_blocks": [source["CidrIpv6"]] if "CidrIpv6" in source else [],
        "source_security_group_id": source.get("GroupId"),
        # AWS stores the description per range; it must be read back to match
        # declared rules, including `ALLOW_ALL_EGRESS`, without spurious drift.
        "description": description(unit),
    }


def _source(unit: dict[str, Any]) -> dict[str, Any]:
    """A one-range unit's only range or group pair, or ``{}`` if it has none."""
    entries = [
        *unit.get("IpRanges", []),
        *unit.get("Ipv6Ranges", []),
        *unit.get("UserIdGroupPairs", []),
    ]
    return entries[0] if entries else {}


def description(unit: dict[str, Any]) -> str:
    """The description AWS holds for a one-range unit (see :func:`atomic_units`)."""
    return str(_source(unit).get("Description", ""))


def atomic_units(permissions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each ``IpPermission`` split into one-range units.

    EC2 merges every range with the same ``(protocol, from, to)`` into one live
    permission, so whole-permission comparison re-authorizes present ranges
    (``InvalidPermission.Duplicate``) and revokes merged rules wholesale. Units
    are the grain EC2 authorizes and revokes at.
    """
    units: list[dict[str, Any]] = []
    for permission in permissions:
        # Annotated: the literal key tuple otherwise infers `Literal[...]` keys,
        # which do not unpack into a `str`-keyed dict.
        base: dict[str, Any] = {
            k: permission[k] for k in ("IpProtocol", "FromPort", "ToPort") if k in permission
        }
        units.extend({**base, "IpRanges": [entry]} for entry in permission.get("IpRanges", []))
        units.extend({**base, "Ipv6Ranges": [entry]} for entry in permission.get("Ipv6Ranges", []))
        units.extend(
            {**base, "UserIdGroupPairs": [entry]}
            for entry in permission.get("UserIdGroupPairs", [])
        )
        if not (
            permission.get("IpRanges")
            or permission.get("Ipv6Ranges")
            or permission.get("UserIdGroupPairs")
        ):
            units.append(dict(base))
    return units


def identity(permission: dict[str, Any]) -> tuple[Any, ...]:
    """What makes two rules the same rule, ignoring description.

    Description is range metadata, not part of what the rule permits; including it
    would revoke and re-authorize a rule whenever its description changed.
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


def redescribed(
    desired: list[dict[str, Any]], current: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The ``desired`` units present live under a different description.

    :func:`identity` ignores descriptions, so the authorize/revoke delta never
    applies a description-only edit; these units go through
    ``update_security_group_rule_descriptions_*`` instead.
    """
    return [
        unit
        for unit in desired
        if any(
            identity(live) == identity(unit) and description(live) != description(unit)
            for live in current
        )
    ]
