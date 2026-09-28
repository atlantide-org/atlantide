"""EC2 networking: VPCs, subnets, security groups, gateways and route tables.

Enough AWS to host a workload. ``Vpc`` + ``Subnet`` + ``SecurityGroup`` alone can
describe a network that looks right and carries no traffic (a group with no rules
is permanently default-deny, and a subnet with no route to a gateway reaches
nothing), so most of these tests cover the pieces that make the network route.

Runs in :data:`~tests.support.TEST_REGION` (the conftest default is us-east-1):
the helpers and several assertions name that region explicitly.
"""

from __future__ import annotations

from pathlib import Path

import boto3
import pytest

from atlantide.core import Context
from atlantide.providers.aws import (
    ALLOW_ALL_EGRESS,
    AwsProvider,
    ElasticIp,
    InternetGateway,
    NatGateway,
    Route,
    RouteTable,
    SecurityGroup,
    SgRule,
    Subnet,
    Vpc,
)
from tests.support import TEST_REGION, Cli, aws_fixture

cli = Cli()

aws_env = aws_fixture()


async def _vpc(provider: AwsProvider, cidr: str = "10.0.0.0/16") -> str:
    out = await provider.create(Context(), Vpc("v", cidr_block=cidr))
    return str(out["vpc_id"])


def _live_group(group_id: str) -> dict:
    return boto3.client("ec2", region_name=TEST_REGION).describe_security_groups(
        GroupIds=[group_id]
    )["SecurityGroups"][0]


async def test_ec2_vpc_subnet_security_group() -> None:
    provider = AwsProvider()
    vpc_out = await provider.create(Context(), Vpc("v", cidr_block="10.0.0.0/16"))
    assert vpc_out["vpc_id"].startswith("vpc-")

    subnet = Subnet("s", vpc_id=vpc_out["vpc_id"], cidr_block="10.0.1.0/24")
    subnet_out = await provider.create(Context(), subnet)
    assert subnet_out["subnet_id"].startswith("subnet-")
    assert await provider.read(Context(), subnet) is not None

    sg = SecurityGroup("g", group_name="web", vpc_id=vpc_out["vpc_id"])
    sg_out = await provider.create(Context(), sg)
    assert sg_out["group_id"].startswith("sg-")

    await provider.delete(Context(), sg)
    await provider.delete(Context(), subnet)
    assert await provider.read(Context(), subnet) is None
    await provider.delete(Context(), Vpc("v", cidr_block="10.0.0.0/16"))


async def test_delete_targets_state_id_not_shared_cidr() -> None:
    # Many accounts hold several 10.0.0.0/16 VPCs. Delete must act on the id state
    # recorded, never re-discover by the (non-unique) CIDR and hit a different VPC.
    provider = AwsProvider()
    ec2 = boto3.client("ec2")
    bystander = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    ours = (await provider.create(Context(), Vpc("v", cidr_block="10.0.0.0/16")))["vpc_id"]
    assert bystander != ours

    # a resource reconstructed from state carries its real id on the computed field.
    await provider.delete(Context(), Vpc("v", cidr_block="10.0.0.0/16", vpc_id=ours))

    live = {v["VpcId"] for v in ec2.describe_vpcs()["Vpcs"]}
    assert ours not in live  # ours (by its id) was deleted
    assert bystander in live  # the shared-CIDR VPC was left untouched


# -- create idempotency -----------------------------------------------------
#
# A create is re-run whenever its state row never reached `created`: the process
# was killed between the AWS call and the persist, or a sibling node failed and
# cancelled the task. By then the resource may exist with nothing recording it,
# so an unconditional create either duplicates it or fails on a name clash.


async def test_ec2_create_adopts_its_own_interrupted_create() -> None:
    provider = AwsProvider()
    vpc = Vpc("v", cidr_block="10.0.0.0/16")
    first = (await provider.create(Context(), vpc))["vpc_id"]

    again = (await provider.create(Context(), vpc))["vpc_id"]
    assert again == first, "the retry must adopt, not make a second VPC"


async def test_ec2_create_does_not_adopt_a_stranger_sharing_a_cidr() -> None:
    """EC2 attributes are not unique, so adoption is keyed on the node tag."""
    provider = AwsProvider()
    boto3.client("ec2").create_vpc(CidrBlock="10.0.0.0/16")  # someone else's

    ours = (await provider.create(Context(), Vpc("v", cidr_block="10.0.0.0/16")))["vpc_id"]
    vpcs = boto3.client("ec2").describe_vpcs()["Vpcs"]
    assert sum(1 for v in vpcs if v["CidrBlock"] == "10.0.0.0/16") == 2
    assert ours in {v["VpcId"] for v in vpcs}


# -- security group rules --------------------------------------------------------


async def test_a_group_with_no_rules_is_still_created() -> None:
    """A group is valid before anyone has decided what it permits."""
    provider = AwsProvider()
    vpc_id = await _vpc(provider)
    out = await provider.create(
        Context(), SecurityGroup("g", group_name="bare", vpc_id=vpc_id, egress=[])
    )
    assert out["group_id"].startswith("sg-")


async def test_declared_ingress_is_authorized() -> None:
    """Declared ingress is authorized, so a group is not stuck at
    default-deny-inbound."""
    provider = AwsProvider()
    vpc_id = await _vpc(provider)

    out = await provider.create(
        Context(),
        SecurityGroup(
            "g",
            group_name="web",
            vpc_id=vpc_id,
            ingress=[SgRule(protocol="tcp", from_port=443, to_port=443, cidr_blocks=["0.0.0.0/0"])],
        ),
    )

    permissions = _live_group(out["group_id"])["IpPermissions"]
    assert any(p["FromPort"] == 443 for p in permissions)


async def test_removing_a_rule_revokes_it() -> None:
    """Without a revoke, deleting a rule from config leaves the port open, and
    nothing breaks to reveal it."""
    provider = AwsProvider()
    vpc_id = await _vpc(provider)
    opened = SecurityGroup(
        "g",
        group_name="web",
        vpc_id=vpc_id,
        ingress=[
            SgRule(protocol="tcp", from_port=443, to_port=443, cidr_blocks=["0.0.0.0/0"]),
            SgRule(protocol="tcp", from_port=22, to_port=22, cidr_blocks=["10.0.0.0/8"]),
        ],
    )
    out = await provider.create(Context(), opened)

    closed = SecurityGroup(
        "g",
        group_name="web",
        vpc_id=vpc_id,
        ingress=[SgRule(protocol="tcp", from_port=443, to_port=443, cidr_blocks=["0.0.0.0/0"])],
    )
    await provider.update(Context(), out, closed)

    ports = {p["FromPort"] for p in _live_group(out["group_id"])["IpPermissions"]}
    assert ports == {443}, "the ssh rule was revoked, not merely un-declared"


async def test_egress_defaults_to_open_as_aws_does() -> None:
    """Matches the AWS default, so outbound traffic does not silently stop for a
    group that declares no egress."""
    provider = AwsProvider()
    vpc_id = await _vpc(provider)
    out = await provider.create(
        Context(), SecurityGroup("g", group_name="default-egress", vpc_id=vpc_id)
    )
    assert _live_group(out["group_id"])["IpPermissionsEgress"]


async def test_an_empty_egress_list_revokes_the_implicit_rule() -> None:
    """`egress=[]` has to *revoke* something: a new group is born with allow-all,
    so not adding a rule is not the same as denying."""
    provider = AwsProvider()
    vpc_id = await _vpc(provider)
    out = await provider.create(
        Context(), SecurityGroup("g", group_name="locked", vpc_id=vpc_id, egress=[])
    )
    assert _live_group(out["group_id"])["IpPermissionsEgress"] == []


async def test_a_group_can_reference_another_group() -> None:
    """Group-to-group access. Passing a `Ref` here is what makes it a dependency
    edge rather than a string nobody ordered."""
    provider = AwsProvider()
    vpc_id = await _vpc(provider)
    upstream = await provider.create(
        Context(), SecurityGroup("a", group_name="db", vpc_id=vpc_id, egress=[])
    )

    out = await provider.create(
        Context(),
        SecurityGroup(
            "b",
            group_name="app",
            vpc_id=vpc_id,
            egress=[],
            ingress=[
                SgRule(
                    protocol="tcp",
                    from_port=5432,
                    to_port=5432,
                    source_security_group_id=upstream["group_id"],
                )
            ],
        ),
    )

    pairs = _live_group(out["group_id"])["IpPermissions"][0]["UserIdGroupPairs"]
    assert pairs[0]["GroupId"] == upstream["group_id"]


async def test_a_group_read_reports_its_rules() -> None:
    """A port opened in the console must show as drift rather than a blanket
    "in sync" that checked nothing."""
    provider = AwsProvider()
    vpc_id = await _vpc(provider)
    group = SecurityGroup(
        "g",
        group_name="observed",
        vpc_id=vpc_id,
        egress=[],
        ingress=[SgRule(protocol="tcp", from_port=80, to_port=80, cidr_blocks=["0.0.0.0/0"])],
    )
    await provider.create(Context(), group)

    live = await provider.read(Context(), group)

    assert live is not None
    assert live["ingress"][0]["from_port"] == 80


def test_a_rule_needs_ports_unless_it_covers_every_protocol() -> None:
    with pytest.raises(ValueError, match="from_port and to_port"):
        SgRule(protocol="tcp", cidr_blocks=["0.0.0.0/0"])
    # all traffic: ports are meaningless and not required
    SgRule(protocol="-1", cidr_blocks=["0.0.0.0/0"])


def test_a_rule_needs_a_source() -> None:
    """A sourceless rule would reach AWS as an ``IpPermission`` with no ranges."""
    with pytest.raises(ValueError, match="needs a source"):
        SgRule(protocol="tcp", from_port=443, to_port=443)
    SgRule(protocol="tcp", from_port=443, to_port=443, ipv6_cidr_blocks=["::/0"])
    SgRule(protocol="-1", source_security_group_id="sg-1")
    SgRule(protocol="-1", cidr_blocks=["10.0.0.0/8"], source_security_group_id="sg-1")


def test_the_allow_all_egress_constant_is_what_the_default_uses() -> None:
    """So a config can say "everything except this" without respelling it."""
    assert SecurityGroup("g", group_name="x", vpc_id="vpc-1", region=TEST_REGION).egress == [
        ALLOW_ALL_EGRESS
    ]


# -- subnets ---------------------------------------------------------------------


async def test_a_subnet_lands_in_the_zone_it_names() -> None:
    """Without this, AWS picks the zone, and two subnets meant for different
    zones can land in the same one, defeating a multi-AZ deployment."""
    provider = AwsProvider()
    vpc_id = await _vpc(provider)
    zone = f"{TEST_REGION}b"

    out = await provider.create(
        Context(),
        Subnet("s", vpc_id=vpc_id, cidr_block="10.0.1.0/24", availability_zone=zone),
    )

    live = boto3.client("ec2", region_name=TEST_REGION).describe_subnets(
        SubnetIds=[out["subnet_id"]]
    )["Subnets"][0]
    assert live["AvailabilityZone"] == zone


async def test_public_ip_mapping_is_off_unless_asked_for() -> None:
    """A subnet hands out public addresses only when config says so."""
    provider = AwsProvider()
    vpc_id = await _vpc(provider)
    out = await provider.create(Context(), Subnet("s", vpc_id=vpc_id, cidr_block="10.0.2.0/24"))
    live = await provider.read(
        Context(),
        Subnet("s", vpc_id=vpc_id, cidr_block="10.0.2.0/24", subnet_id=out["subnet_id"]),
    )
    assert live is not None and live["map_public_ip_on_launch"] is False


# -- a network that routes -------------------------------------------------------


async def test_an_internet_gateway_is_attached_on_create() -> None:
    """An unattached gateway does nothing, which is why the attachment is a field
    rather than a resource of its own."""
    provider = AwsProvider()
    vpc_id = await _vpc(provider)

    out = await provider.create(Context(), InternetGateway("igw", vpc_id=vpc_id))

    live = boto3.client("ec2", region_name=TEST_REGION).describe_internet_gateways(
        InternetGatewayIds=[out["internet_gateway_id"]]
    )["InternetGateways"][0]
    assert live["Attachments"][0]["VpcId"] == vpc_id


async def test_a_route_table_sends_traffic_to_a_gateway() -> None:
    """The piece that turns a subnet into a public one."""
    provider = AwsProvider()
    vpc_id = await _vpc(provider)
    igw = await provider.create(Context(), InternetGateway("igw", vpc_id=vpc_id))
    subnet = await provider.create(Context(), Subnet("s", vpc_id=vpc_id, cidr_block="10.0.1.0/24"))

    out = await provider.create(
        Context(),
        RouteTable(
            "rt",
            vpc_id=vpc_id,
            routes=[Route(cidr_block="0.0.0.0/0", gateway_id=igw["internet_gateway_id"])],
            subnet_ids=[subnet["subnet_id"]],
        ),
    )

    live = boto3.client("ec2", region_name=TEST_REGION).describe_route_tables(
        RouteTableIds=[out["route_table_id"]]
    )["RouteTables"][0]
    assert any(
        r.get("DestinationCidrBlock") == "0.0.0.0/0"
        and r.get("GatewayId") == igw["internet_gateway_id"]
        for r in live["Routes"]
    )
    assert live["Associations"][0]["SubnetId"] == subnet["subnet_id"]


async def test_removing_a_route_deletes_it() -> None:
    provider = AwsProvider()
    vpc_id = await _vpc(provider)
    igw = await provider.create(Context(), InternetGateway("igw", vpc_id=vpc_id))
    table = RouteTable(
        "rt",
        vpc_id=vpc_id,
        routes=[Route(gateway_id=igw["internet_gateway_id"])],
    )
    out = await provider.create(Context(), table)

    await provider.update(Context(), out, RouteTable("rt", vpc_id=vpc_id, routes=[]))

    live = boto3.client("ec2", region_name=TEST_REGION).describe_route_tables(
        RouteTableIds=[out["route_table_id"]]
    )["RouteTables"][0]
    assert not any(r.get("DestinationCidrBlock") == "0.0.0.0/0" for r in live["Routes"])


async def test_the_local_route_is_never_removed() -> None:
    """It is created with the table and cannot be deleted; treating it as
    undeclared would make every update fail."""
    provider = AwsProvider()
    vpc_id = await _vpc(provider)
    out = await provider.create(Context(), RouteTable("rt", vpc_id=vpc_id, routes=[]))

    live = boto3.client("ec2", region_name=TEST_REGION).describe_route_tables(
        RouteTableIds=[out["route_table_id"]]
    )["RouteTables"][0]
    assert any(r.get("GatewayId") == "local" for r in live["Routes"])


async def test_an_elastic_ip_reports_its_address() -> None:
    provider = AwsProvider()
    out = await provider.create(Context(), ElasticIp("eip"))
    assert out["allocation_id"]
    assert out["public_ip"].count(".") == 3


async def test_a_nat_gateway_sits_behind_an_elastic_ip() -> None:
    """The private-subnet egress path, end to end."""
    provider = AwsProvider()
    vpc_id = await _vpc(provider)
    public = await provider.create(
        Context(), Subnet("public", vpc_id=vpc_id, cidr_block="10.0.1.0/24")
    )
    eip = await provider.create(Context(), ElasticIp("eip"))

    out = await provider.create(
        Context(),
        NatGateway("nat", subnet_id=public["subnet_id"], allocation_id=eip["allocation_id"]),
    )

    assert out["nat_gateway_id"].startswith("nat-")


def test_a_route_needs_exactly_one_target() -> None:
    """Two targets is ambiguous and none is inert; both are config mistakes
    caught before an apply."""
    with pytest.raises(ValueError, match="exactly one"):
        Route()
    with pytest.raises(ValueError, match="exactly one"):
        Route(gateway_id="igw-1", nat_gateway_id="nat-1")


def test_a_whole_routable_network_compiles(tmp_path: Path) -> None:
    """The shape a real deployment needs, expressed end to end."""
    cfg = tmp_path / "net.py"
    cfg.write_text(
        "from atlantide.providers.aws import (\n"
        "    ElasticIp, InternetGateway, NatGateway, Route, RouteTable,\n"
        "    SecurityGroup, SgRule, Subnet, Vpc,\n"
        ")\n"
        f"vpc = Vpc('vpc', cidr_block='10.0.0.0/16', region={TEST_REGION!r})\n"
        f"igw = InternetGateway('igw', vpc_id=vpc.vpc_id, region={TEST_REGION!r})\n"
        "public = Subnet('public', vpc_id=vpc.vpc_id, cidr_block='10.0.1.0/24',\n"
        f"                map_public_ip_on_launch=True, region={TEST_REGION!r})\n"
        "private = Subnet('private', vpc_id=vpc.vpc_id, cidr_block='10.0.2.0/24',\n"
        f"                 region={TEST_REGION!r})\n"
        f"eip = ElasticIp('eip', region={TEST_REGION!r})\n"
        "nat = NatGateway('nat', subnet_id=public.subnet_id,\n"
        f"                 allocation_id=eip.allocation_id, region={TEST_REGION!r})\n"
        "RouteTable('public-rt', vpc_id=vpc.vpc_id,\n"
        "           routes=[Route(gateway_id=igw.internet_gateway_id)],\n"
        f"           subnet_ids=[public.subnet_id], region={TEST_REGION!r})\n"
        "RouteTable('private-rt', vpc_id=vpc.vpc_id,\n"
        "           routes=[Route(nat_gateway_id=nat.nat_gateway_id)],\n"
        f"           subnet_ids=[private.subnet_id], region={TEST_REGION!r})\n"
        "SecurityGroup('web', group_name='web', vpc_id=vpc.vpc_id,\n"
        "              ingress=[SgRule(protocol='tcp', from_port=443, to_port=443,\n"
        "                              cidr_blocks=['0.0.0.0/0'])],\n"
        f"              region={TEST_REGION!r})\n"
    )

    result = cli.ok("validate", cfg)
    assert "9 resource(s)" in result.output


# -- regression: rule merging, route repointing, delete safety -------------------


async def test_two_rules_on_one_port_with_different_cidrs_converge() -> None:
    """EC2 coalesces live ranges per (protocol, from, to); the sync must compare
    per atomic range or the second apply re-authorizes already-present ranges
    (InvalidPermission.Duplicate) and revokes the merged live rule wholesale."""
    provider = AwsProvider()
    vpc_id = await _vpc(provider)
    rules = [
        SgRule(protocol="tcp", from_port=443, to_port=443, cidr_blocks=["10.1.0.0/16"]),
        SgRule(protocol="tcp", from_port=443, to_port=443, cidr_blocks=["10.2.0.0/16"]),
    ]
    group = SecurityGroup("g", group_name="web", vpc_id=vpc_id, ingress=rules, egress=[])
    out = await provider.create(Context(), group)

    await provider.update(Context(), out, group)  # re-sync against merged live rules

    live = _live_group(str(out["group_id"]))
    ranges = {r["CidrIp"] for p in live["IpPermissions"] for r in p.get("IpRanges", [])}
    assert ranges == {"10.1.0.0/16", "10.2.0.0/16"}


async def test_repointing_a_route_replaces_its_target() -> None:
    """Changing a route's target must replace it; a suppressed `create_route`
    error would leave the old target in place."""
    provider = AwsProvider()
    vpc_id = await _vpc(provider)
    igw = await provider.create(Context(), InternetGateway("igw", vpc_id=vpc_id))
    subnet = await provider.create(Context(), Subnet("s", vpc_id=vpc_id, cidr_block="10.0.1.0/24"))
    eip = await provider.create(Context(), ElasticIp("ip"))
    nat = await provider.create(
        Context(),
        NatGateway(
            "nat",
            subnet_id=str(subnet["subnet_id"]),
            allocation_id=str(eip["allocation_id"]),
        ),
    )
    out = await provider.create(
        Context(),
        RouteTable("rt", vpc_id=vpc_id, routes=[Route(gateway_id=str(igw["internet_gateway_id"]))]),
    )

    await provider.update(
        Context(),
        out,
        RouteTable("rt", vpc_id=vpc_id, routes=[Route(nat_gateway_id=str(nat["nat_gateway_id"]))]),
    )

    live = boto3.client("ec2", region_name=TEST_REGION).describe_route_tables(
        RouteTableIds=[str(out["route_table_id"])]
    )["RouteTables"][0]
    default = next(r for r in live["Routes"] if r.get("DestinationCidrBlock") == "0.0.0.0/0")
    assert default.get("NatGatewayId") == nat["nat_gateway_id"]
    assert default.get("GatewayId") is None


async def test_destroying_an_already_gone_resource_is_a_noop() -> None:
    """Out-of-band deletion must not fail the destroy (idempotent delete)."""
    provider = AwsProvider()
    vpc_id = await _vpc(provider)
    boto3.client("ec2", region_name=TEST_REGION).delete_vpc(VpcId=vpc_id)

    await provider.delete(Context(), Vpc("v", cidr_block="10.0.0.0/16", vpc_id=vpc_id))


async def test_destroy_never_attribute_matches_an_unmanaged_resource() -> None:
    """A creating-status row with no id (create never reached AWS) must not
    attribute-match — and delete — a pre-existing unmanaged VPC with the same
    CIDR. The create path refuses attribute matching for the same reason."""
    provider = AwsProvider()
    client = boto3.client("ec2", region_name=TEST_REGION)
    unmanaged = client.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]

    await provider.delete(Context(), Vpc("v", cidr_block="10.0.0.0/16"))

    assert client.describe_vpcs(VpcIds=[unmanaged])["Vpcs"], "unmanaged VPC must survive"


async def test_a_removed_tag_is_deleted_on_update() -> None:
    """EC2 tagging is additive; dropping a tag from config must untag it."""
    provider = AwsProvider()
    out = await provider.create(
        Context(), Vpc("v", cidr_block="10.0.0.0/16", tags={"env": "dev", "owner": "a"})
    )
    vpc_id = str(out["vpc_id"])

    await provider.update(
        Context(), out, Vpc("v", cidr_block="10.0.0.0/16", tags={"env": "dev"}, vpc_id=vpc_id)
    )

    live = boto3.client("ec2", region_name=TEST_REGION).describe_vpcs(VpcIds=[vpc_id])["Vpcs"][0]
    tags = {t["Key"]: t["Value"] for t in live.get("Tags", [])}
    assert "owner" not in tags and tags["env"] == "dev"
