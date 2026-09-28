"""EC2 reads resolve the id state recorded, not whatever matches the attributes.

Like ``update`` and ``delete``, ``read`` acts on the persisted id. Attribute
lookup gives the right answer in the ordinary case (one VPC, one CIDR, no
out-of-band edits) and the wrong answer in two cases, in opposite directions:

* an account holding two 10.0.0.0/16 VPCs answers with whichever the API returns
  first, so refresh silently reports on a resource this node does not own;
* a VPC whose CIDR was changed out of band matches nothing, so refresh reports
  MISSING for a live resource, and ``refresh --write --prune`` would drop the
  only record of it.

Neither shows in the output, so these tests pin both.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from atlantide.core import Context
from atlantide.providers.aws import AwsProvider, ElasticIp, RouteTable, Vpc
from atlantide.providers.aws.handlers import HANDLERS
from atlantide.providers.aws.handlers.base import is_missing
from atlantide.providers.aws.handlers.networking import VpcHandler
from tests.providers.aws.conftest import ec2_client
from tests.support import aws_fixture

aws_env = aws_fixture()


async def test_a_second_vpc_sharing_the_cidr_does_not_stand_in_for_a_deleted_one() -> None:
    """The wrong-resource direction.

    Attribute lookup cannot tell two VPCs with the same CIDR apart, so the
    deleted VPC this node owns would read as present, matched to the other one.
    """
    provider = AwsProvider()
    created = await provider.create(Context(), Vpc("v", cidr_block="10.0.0.0/16"))
    vpc_id = str(created["vpc_id"])
    # A second VPC with the same CIDR, not owned by this node.
    decoy = ec2_client().create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    assert decoy != vpc_id
    ec2_client().delete_vpc(VpcId=vpc_id)

    # State restores the recorded id onto the computed field, which is what a
    # refresh hands the provider.
    live = await provider.read(Context(), Vpc("v", cidr_block="10.0.0.0/16", vpc_id=vpc_id))
    assert live is None


async def test_a_cidr_changed_out_of_band_does_not_read_as_missing() -> None:
    """The phantom-drift direction.

    The resource still exists; only the attribute an attribute lookup keys on
    changed. A read by id is unaffected, which is why the id is recorded.
    """
    provider = AwsProvider()
    created = await provider.create(Context(), Vpc("v", cidr_block="10.0.0.0/16"))
    vpc_id = str(created["vpc_id"])

    # Config still says 10.0.0.0/16; nothing in the account does.
    stale = Vpc("v", cidr_block="10.99.0.0/16", vpc_id=vpc_id)
    live = await provider.read(Context(), stale)
    assert live is not None
    assert live["vpc_id"] == vpc_id


async def test_a_vpc_with_no_recorded_id_still_falls_back_to_attributes() -> None:
    """A create interrupted before its state write leaves a resource with no
    recorded id; `_find` locates it by attributes."""
    provider = AwsProvider()
    created = await provider.create(Context(), Vpc("v", cidr_block="10.7.0.0/16"))
    live = await provider.read(Context(), Vpc("v", cidr_block="10.7.0.0/16"))
    assert live is not None
    assert live["vpc_id"] == created["vpc_id"]


@pytest.mark.parametrize("cls", [ElasticIp, RouteTable])
async def test_tag_only_resources_are_readable_by_a_recorded_id(cls: type) -> None:
    """An address and a route table have no attributes of their own to match on,
    so their lookup is the managed-tag filter. Without a read by id, a resource
    carrying no atlantide tag could never be read, and so could not be adopted."""
    provider = AwsProvider()
    if cls is ElasticIp:
        resource_id = ec2_client().allocate_address(Domain="vpc")["AllocationId"]
        untagged = ElasticIp("e", allocation_id=resource_id)
        field = "allocation_id"
    else:
        vpc_id = ec2_client().create_vpc(CidrBlock="10.3.0.0/16")["Vpc"]["VpcId"]
        resource_id = ec2_client().create_route_table(VpcId=vpc_id)["RouteTable"]["RouteTableId"]
        untagged = RouteTable("r", vpc_id=vpc_id, route_table_id=resource_id)
        field = "route_table_id"

    # Nothing tagged it, so the attribute lookup these types use finds nothing.
    handler = HANDLERS[untagged.type_name()]
    assert handler._find_tagged(ec2_client(), untagged.node_id) is None  # type: ignore[attr-defined]

    live = await provider.read(Context(), untagged)
    assert live is not None
    assert live[field] == resource_id


async def test_a_recorded_id_that_no_longer_exists_reads_as_missing() -> None:
    """A read by id must still report "gone", or it would trade phantom drift for
    phantom presence."""
    provider = AwsProvider()
    live = await provider.read(Context(), Vpc("v", cidr_block="10.0.0.0/16", vpc_id="vpc-00000000"))
    assert live is None


def test_a_denied_read_is_not_reported_as_absence() -> None:
    """The distinction `is_missing` exists to protect.

    An absent resource and one the caller may not see both arrive as a
    `ClientError`. Treating the second as the first makes `refresh --prune` delete
    the only record of a live resource. The suffix rule broadens what counts as
    absence, so this pins what does not.
    """

    class _Raising:
        def __init__(self, err: ClientError) -> None:
            self._err = err

        def describe_vpcs(self, **_: object) -> dict[str, object]:
            raise self._err

    for code in ("UnauthorizedOperation", "RequestLimitExceeded", "InternalError"):
        exc = ClientError({"Error": {"Code": code}}, "DescribeVpcs")
        assert not is_missing(exc)
        with pytest.raises(ClientError):
            VpcHandler()._describe(_Raising(exc), "vpc-123")


def test_ec2s_per_type_absence_codes_all_count_as_missing() -> None:
    """EC2 spells absence once per resource type, which is why the predicate
    matches the suffix rather than enumerating a set that AWS keeps extending."""
    for code in (
        "InvalidVpcID.NotFound",
        "InvalidSubnetID.NotFound",
        "InvalidGroup.NotFound",
        "InvalidAllocationID.NotFound",
        "InvalidRouteTableID.NotFound",
        "NatGatewayNotFound",
    ):
        assert is_missing(ClientError({"Error": {"Code": code}}, "Describe"))
