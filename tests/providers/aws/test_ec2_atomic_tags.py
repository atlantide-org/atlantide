"""The EC2 identity tag is atomic with the create, not applied after it.

EC2 resources are adopted on re-run by :data:`MANAGED_TAG` (see ``Ec2Handler``).
If the tag were applied by ``ec2_tag`` after ``_create`` returned, a crash or a
transport-level retry of the whole create in between would leave a live resource
``_find_tagged`` cannot see, and the re-run would provision a duplicate. These
tests exercise that window: ``_create`` alone, with post-create tagging never
run, must be findable by the node tag.
"""

from __future__ import annotations

from atlantide.providers.aws import (
    ElasticIp,
    InternetGateway,
    NatGateway,
    RouteTable,
    SecurityGroup,
    Subnet,
    Vpc,
)
from atlantide.providers.aws.handlers.networking import (
    ElasticIpHandler,
    InternetGatewayHandler,
    NatGatewayHandler,
    RouteTableHandler,
    SecurityGroupHandler,
    SubnetHandler,
    VpcHandler,
)
from tests.providers.aws.conftest import ec2_client
from tests.support import aws_fixture

aws_env = aws_fixture()


def test_every_bare_create_is_findable_by_the_node_tag() -> None:
    """Each handler's ``_create``, with ``ec2_tag`` deliberately never run."""
    client = ec2_client()

    vpc = Vpc("v", cidr_block="10.0.0.0/16")
    vpc_id = VpcHandler()._create(client, vpc)
    assert VpcHandler()._find_tagged(client, vpc.node_id) == vpc_id

    subnet = Subnet("s", vpc_id=vpc_id, cidr_block="10.0.1.0/24")
    subnet_id = SubnetHandler()._create(client, subnet)
    assert SubnetHandler()._find_tagged(client, subnet.node_id) == subnet_id

    group = SecurityGroup("g", group_name="web", vpc_id=vpc_id)
    group_id = SecurityGroupHandler()._create(client, group)
    assert SecurityGroupHandler()._find_tagged(client, group.node_id) == group_id

    table = RouteTable("rt", vpc_id=vpc_id)
    table_id = RouteTableHandler()._create(client, table)
    assert RouteTableHandler()._find_tagged(client, table.node_id) == table_id

    igw = InternetGateway("igw", vpc_id=vpc_id)
    igw_id = InternetGatewayHandler()._create(client, igw)
    assert InternetGatewayHandler()._find_tagged(client, igw.node_id) == igw_id

    eip = ElasticIp("ip")
    allocation_id = ElasticIpHandler()._create(client, eip)
    assert ElasticIpHandler()._find_tagged(client, eip.node_id) == allocation_id

    nat = NatGateway("nat", subnet_id=subnet_id, allocation_id=allocation_id)
    nat_id = NatGatewayHandler()._create(client, nat)
    assert NatGatewayHandler()._find_tagged(client, nat.node_id) == nat_id


def test_a_rerun_create_after_the_crash_window_adopts_not_duplicates() -> None:
    """The re-run's full ``create`` adopts the interrupted VPC instead of
    provisioning a second one."""
    client = ec2_client()
    handler = VpcHandler()
    vpc = Vpc("v", cidr_block="10.0.0.0/16")
    created = handler._create(client, vpc)  # the crash: ec2_tag never runs

    out = handler.create(client, vpc)

    assert out["vpc_id"] == created
    vpcs = client.describe_vpcs(Filters=[{"Name": "cidr", "Values": ["10.0.0.0/16"]}])["Vpcs"]
    assert len(vpcs) == 1, "the re-run adopted rather than duplicated"
