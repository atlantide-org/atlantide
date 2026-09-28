"""Route53 hosted zones and record sets, plain and alias."""

from __future__ import annotations

import boto3
import pytest

from atlantide.core import Context
from atlantide.providers.aws import AwsProvider, Route53HostedZone, Route53Record

pytestmark = pytest.mark.usefixtures("aws_env")


async def test_route53_hosted_zone_crud() -> None:
    provider = AwsProvider()
    ctx = Context()
    out = await provider.create(ctx, Route53HostedZone("z", domain="example.com", comment="v1"))
    zid = out["zone_id"]
    assert zid and out["name_servers"]
    tracked = Route53HostedZone("z", domain="example.com", zone_id=zid)
    assert await provider.read(ctx, tracked) is not None
    await provider.update(
        ctx,
        {"zone_id": zid},
        Route53HostedZone("z", domain="example.com", comment="v2", zone_id=zid),
    )
    await provider.delete(ctx, tracked)
    assert await provider.read(ctx, tracked) is None


async def test_route53_record_crud() -> None:
    provider = AwsProvider()
    ctx = Context()
    zid = (await provider.create(ctx, Route53HostedZone("z", domain="example.com")))["zone_id"]
    rec = Route53Record(
        "r",
        zone_id=zid,
        record_name="www.example.com",
        record_type="CNAME",
        ttl=300,
        records=["target.cloudfront.net"],
    )
    assert await provider.create(ctx, rec) == {}
    # record_name given without a trailing dot still matches the dotted live name.
    assert await provider.read(ctx, rec) is not None
    await provider.update(
        ctx,
        {},
        Route53Record(
            "r",
            zone_id=zid,
            record_name="www.example.com",
            record_type="CNAME",
            ttl=600,
            records=["target.cloudfront.net"],
        ),
    )
    sets = boto3.client("route53").list_resource_record_sets(HostedZoneId=zid)["ResourceRecordSets"]
    assert next(s["TTL"] for s in sets if s["Type"] == "CNAME") == 600
    await provider.delete(ctx, rec)  # deletes by the exact live set (ttl 600)
    assert await provider.read(ctx, rec) is None


# -- alias records ---------------------------------------------------------------


def test_an_alias_record_points_at_a_target_rather_than_an_address() -> None:
    """An apex A-record to CloudFront requires an alias: CloudFront has no fixed
    IP, and DNS forbids a CNAME at the apex."""
    from atlantide.providers.aws.handlers.dns import _record_set
    from atlantide.providers.aws.resources.dns import (
        CLOUDFRONT_ZONE_ID,
        AliasTarget,
        Route53Record,
    )

    record_set = _record_set(
        Route53Record(
            "r",
            zone_id="Z1",
            record_name="example.com",
            record_type="A",
            alias=AliasTarget(name="d123.cloudfront.net", zone_id=CLOUDFRONT_ZONE_ID),
        )
    )

    assert record_set["AliasTarget"]["DNSName"] == "d123.cloudfront.net"
    assert "TTL" not in record_set, "Route 53 rejects a set carrying both"
    assert "ResourceRecords" not in record_set


def test_a_plain_record_still_carries_its_ttl_and_values() -> None:
    from atlantide.providers.aws.handlers.dns import _record_set
    from atlantide.providers.aws.resources.dns import Route53Record

    record_set = _record_set(
        Route53Record("r", zone_id="Z1", record_name="a.example.com", records=["1.2.3.4"])
    )
    assert record_set["TTL"] == 300
    assert record_set["ResourceRecords"] == [{"Value": "1.2.3.4"}]


def test_a_record_cannot_be_both_kinds_at_once() -> None:
    from atlantide.providers.aws.resources.dns import AliasTarget, Route53Record

    with pytest.raises(ValueError, match="either records or alias"):
        Route53Record(
            "r",
            zone_id="Z1",
            record_name="example.com",
            records=["1.2.3.4"],
            alias=AliasTarget(name="d.cloudfront.net", zone_id="Z2"),
        )
