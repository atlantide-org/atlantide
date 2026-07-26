"""EC2 reads resolve the id state recorded, not whatever matches the attributes.

``update`` and ``delete`` always acted on the persisted id and said so; ``read``
was the one that did not, and it went unnoticed because attribute lookup gives
the right answer in the ordinary case — one VPC, one CIDR, nobody editing
anything in the console. It gives the wrong answer in exactly the two cases that
matter, and wrongly in opposite directions:

* an account holding two 10.0.0.0/16 VPCs answers with whichever the API returns
  first, so refresh silently reports on a resource this node does not own;
* a VPC whose CIDR was changed out of band matches nothing, so refresh reports
  MISSING for a resource that is sitting there — and ``refresh --write --prune``
  would then drop the only record of it.

Both are invisible in the output. These tests are what makes them impossible.
"""

from __future__ import annotations

import boto3
import pytest
from botocore.exceptions import ClientError

from atlantide.core import Context
from atlantide.providers.aws import AwsProvider, ElasticIp, RouteTable, Vpc
from atlantide.providers.aws.handlers import HANDLERS
from atlantide.providers.aws.handlers.base import is_missing
from atlantide.providers.aws.handlers.networking import VpcHandler
from tests.support import TEST_REGION, aws_fixture

aws_env = aws_fixture()


def _ec2():  # type: ignore[no-untyped-def]
    return boto3.client("ec2", region_name=TEST_REGION)


async def test_a_second_vpc_sharing_the_cidr_does_not_stand_in_for_a_deleted_one() -> None:
    """The wrong-resource direction.

    Attribute lookup cannot tell two VPCs with the same CIDR apart, so deleting
    the one this node owns used to read as "still here" — the other one.
    """
    provider = AwsProvider()
    created = await provider.create(Context(), Vpc("v", cidr_block="10.0.0.0/16"))
    vpc_id = str(created["vpc_id"])
    # A second VPC with the same CIDR, owned by nobody in particular.
    decoy = _ec2().create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    assert decoy != vpc_id
    _ec2().delete_vpc(VpcId=vpc_id)

    # State restores the recorded id onto the computed field, which is what a
    # refresh hands the provider.
    live = await provider.read(Context(), Vpc("v", cidr_block="10.0.0.0/16", vpc_id=vpc_id))
    assert live is None


async def test_a_cidr_changed_out_of_band_does_not_read_as_missing() -> None:
    """The phantom-drift direction.

    The resource still exists; only the attribute the old lookup keyed on moved.
    A read by id is indifferent to that, which is the point of recording one.
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
    """The fallback has to stay: a create interrupted before its state write leaves
    a resource with no recorded id, and that is what `_find` is for."""
    provider = AwsProvider()
    created = await provider.create(Context(), Vpc("v", cidr_block="10.7.0.0/16"))
    live = await provider.read(Context(), Vpc("v", cidr_block="10.7.0.0/16"))
    assert live is not None
    assert live["vpc_id"] == created["vpc_id"]


@pytest.mark.parametrize("cls", [ElasticIp, RouteTable])
async def test_tag_only_resources_are_readable_by_a_recorded_id(cls: type) -> None:
    """An address and a route table have no attributes of their own to match on,
    so their lookup is the managed-tag filter. Without a read by id, a resource
    carrying no atlantide tag could never be read at all — which is what would
    have made them impossible to adopt."""
    provider = AwsProvider()
    if cls is ElasticIp:
        resource_id = _ec2().allocate_address(Domain="vpc")["AllocationId"]
        untagged = ElasticIp("e", allocation_id=resource_id)
        field = "allocation_id"
    else:
        vpc_id = _ec2().create_vpc(CidrBlock="10.3.0.0/16")["Vpc"]["VpcId"]
        resource_id = _ec2().create_route_table(VpcId=vpc_id)["RouteTable"]["RouteTableId"]
        untagged = RouteTable("r", vpc_id=vpc_id, route_table_id=resource_id)
        field = "route_table_id"

    # Nothing tagged it, so the attribute lookup these types use finds nothing.
    handler = HANDLERS[untagged.type_name()]
    assert handler._find_tagged(_ec2(), untagged.node_id) is None  # type: ignore[attr-defined]

    live = await provider.read(Context(), untagged)
    assert live is not None
    assert live[field] == resource_id


async def test_a_recorded_id_that_no_longer_exists_reads_as_missing() -> None:
    """A read by id must still be able to say "gone" — otherwise the fix would
    trade phantom drift for phantom presence."""
    provider = AwsProvider()
    live = await provider.read(Context(), Vpc("v", cidr_block="10.0.0.0/16", vpc_id="vpc-00000000"))
    assert live is None


def test_a_denied_read_is_not_reported_as_absence() -> None:
    """The distinction `is_missing` exists to protect.

    An absent resource and one the caller may not see arrive the same way — as a
    `ClientError` — and treating the second as the first is how `refresh --prune`
    deletes the only record of a resource that is sitting right there. The suffix
    rule broadens what counts as absence, so this pins what still does not.
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
