"""Regressions: security-group rule drift and descriptions, NAT gateway readiness,
and ``Marker`` pagination that never advances."""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from botocore.exceptions import WaiterError

from atlantide.core import Context
from atlantide.core.errors import ProviderError
from atlantide.providers.aws import AwsProvider, NatGateway, SecurityGroup, SgRule, Subnet, Vpc
from atlantide.providers.aws.handlers.networking import NatGatewayHandler, SecurityGroupHandler
from atlantide.providers.aws.handlers.pagination import marker_pages
from tests.support import TEST_REGION, aws_fixture

aws_env = aws_fixture()


def _ec2() -> Any:
    return boto3.client("ec2", region_name=TEST_REGION)


async def _vpc(provider: AwsProvider, cidr: str = "10.0.0.0/16") -> str:
    out = await provider.create(Context(), Vpc("v", cidr_block=cidr))
    return str(out["vpc_id"])


async def _group(provider: AwsProvider, ingress: list[SgRule]) -> tuple[SecurityGroup, str]:
    vpc_id = await _vpc(provider)
    group = SecurityGroup("g", group_name="web", vpc_id=vpc_id, ingress=ingress, egress=[])
    out = await provider.create(Context(), group)
    return group, str(out["group_id"])


def _with_id(group: SecurityGroup, group_id: str) -> SecurityGroup:
    return group.model_copy(update={"group_id": group_id})


# -- item 1: no false drift from EC2's rule merging and ordering -----------------


@pytest.mark.parametrize(
    "ingress",
    [
        pytest.param(
            [
                SgRule(protocol="tcp", from_port=80, to_port=80, cidr_blocks=["0.0.0.0/0"]),
                SgRule(protocol="tcp", from_port=443, to_port=443, cidr_blocks=["0.0.0.0/0"]),
            ],
            id="ports-80-then-443",
        ),
        pytest.param(
            [
                SgRule(protocol="tcp", from_port=443, to_port=443, cidr_blocks=["10.1.0.0/16"]),
                SgRule(protocol="tcp", from_port=443, to_port=443, cidr_blocks=["10.2.0.0/16"]),
            ],
            id="same-port-two-rules",
        ),
        pytest.param(
            [
                SgRule(
                    protocol="tcp",
                    from_port=443,
                    to_port=443,
                    cidr_blocks=["10.2.0.0/16", "10.1.0.0/16"],
                )
            ],
            id="unsorted-cidrs",
        ),
    ],
)
async def test_an_untouched_group_reports_its_declared_rules(ingress: list[SgRule]) -> None:
    provider = AwsProvider()
    group, _ = await _group(provider, ingress)

    live = await provider.read(Context(), group)

    assert live is not None
    assert live["ingress"] == [rule.model_dump() for rule in ingress]
    assert live["egress"] == []


async def test_an_out_of_band_rule_still_reads_as_live() -> None:
    provider = AwsProvider()
    declared = [SgRule(protocol="tcp", from_port=443, to_port=443, cidr_blocks=["10.1.0.0/16"])]
    group, group_id = await _group(provider, declared)
    _ec2().authorize_security_group_ingress(
        GroupId=group_id,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 443,
                "ToPort": 443,
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
            }
        ],
    )

    live = await provider.read(Context(), group)

    assert live is not None
    assert live["ingress"] != [rule.model_dump() for rule in declared]
    assert {c for r in live["ingress"] for c in r["cidr_blocks"]} == {"0.0.0.0/0", "10.1.0.0/16"}


# -- item 2: rule descriptions ---------------------------------------------------


async def test_a_description_only_change_is_applied() -> None:
    provider = AwsProvider()
    rule = SgRule(protocol="tcp", from_port=443, to_port=443, cidr_blocks=["10.1.0.0/16"])
    group, group_id = await _group(provider, [rule])

    relabelled = group.model_copy(
        update={"ingress": [rule.model_copy(update={"description": "https from vpn"})]}
    )
    await provider.update(Context(), {"group_id": group_id}, relabelled)

    live = _ec2().describe_security_groups(GroupIds=[group_id])["SecurityGroups"][0]
    ranges = live["IpPermissions"][0]["IpRanges"]
    assert ranges == [{"CidrIp": "10.1.0.0/16", "Description": "https from vpn"}]
    reported = await provider.read(Context(), _with_id(relabelled, group_id))
    assert reported is not None
    assert reported["ingress"] == [r.model_dump() for r in relabelled.ingress]


def test_a_description_is_sent_on_every_range_and_group_pair() -> None:
    """Stubbed: records what reaches EC2 for IPv6 and group-pair sources."""
    calls: dict[str, list[dict[str, Any]]] = {}

    class _Client:
        def describe_security_groups(self, **_: Any) -> dict[str, Any]:
            return {"SecurityGroups": [{"IpPermissions": [], "IpPermissionsEgress": []}]}

        def authorize_security_group_ingress(self, **kwargs: Any) -> None:
            calls["ingress"] = kwargs["IpPermissions"]

        def revoke_security_group_ingress(self, **_: Any) -> None: ...

        authorize_security_group_egress = revoke_security_group_egress = (
            update_security_group_rule_descriptions_ingress
        ) = update_security_group_rule_descriptions_egress = revoke_security_group_ingress

    rule = SgRule(
        protocol="tcp",
        from_port=5432,
        to_port=5432,
        ipv6_cidr_blocks=["::/0"],
        source_security_group_id="sg-1",
        description="db",
    )
    group = SecurityGroup("g", group_name="g", vpc_id="vpc-1", ingress=[rule], egress=[])
    SecurityGroupHandler()._sync_rules(_Client(), "sg-2", group)

    sources = [
        entry
        for unit in calls["ingress"]
        for key in ("Ipv6Ranges", "UserIdGroupPairs")
        for entry in unit.get(key, [])
    ]
    assert sources == [
        {"CidrIpv6": "::/0", "Description": "db"},
        {"GroupId": "sg-1", "Description": "db"},
    ]


# -- item 3: NAT gateway readiness ----------------------------------------------


async def _nat(provider: AwsProvider) -> NatGateway:
    vpc_id = await _vpc(provider)
    subnet = await provider.create(Context(), Subnet("s", vpc_id=vpc_id, cidr_block="10.0.1.0/24"))
    eip = _ec2().allocate_address(Domain="vpc")
    return NatGateway("nat", subnet_id=subnet["subnet_id"], allocation_id=eip["AllocationId"])


class _FailingWaiterClient:
    """The real moto client, except that the ``nat_gateway_available`` waiter
    reports the gateway as failed."""

    def __init__(self) -> None:
        self._client = _ec2()
        self.waited: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)

    def get_waiter(self, name: str) -> Any:
        self.waited.append(name)
        test = self

        class _Waiter:
            def wait(self, **_: Any) -> None:
                raise WaiterError(name, "Waiter encountered a terminal failure state", {})

        return _Waiter() if name == "nat_gateway_available" else test._client.get_waiter(name)


async def test_nat_create_raises_when_the_gateway_fails() -> None:
    nat = await _nat(AwsProvider())
    client = _FailingWaiterClient()

    with pytest.raises(WaiterError):
        NatGatewayHandler().create(client, nat)

    assert client.waited == ["nat_gateway_available"]


async def test_a_failed_nat_gateway_is_not_adopted() -> None:
    from moto.core import DEFAULT_ACCOUNT_ID
    from moto.ec2.models import ec2_backends

    nat = await _nat(AwsProvider())
    handler = NatGatewayHandler()
    first = handler.create(_ec2(), nat)["nat_gateway_id"]
    assert handler._find_tagged(_ec2(), nat.node_id) == first
    ec2_backends[DEFAULT_ACCOUNT_ID][TEST_REGION].nat_gateways[first].state = "failed"

    assert handler._find_tagged(_ec2(), nat.node_id) is None


# -- item 4: Marker pagination ---------------------------------------------------


def test_a_truncated_page_without_a_marker_does_not_loop() -> None:
    pages = iter(
        [
            {"DistributionList": {"Items": [{"Id": "a"}], "IsTruncated": True}},
            {"DistributionList": {"Items": [{"Id": "a"}], "IsTruncated": True}},
        ]
    )

    with pytest.raises(ProviderError, match="NextMarker"):
        list(marker_pages(lambda **_: next(pages), "DistributionList"))
