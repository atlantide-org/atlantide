"""Retagging a bucket must not rebuild the CloudFront distribution in front of it.

``CloudFrontDistribution.origin_domain`` is immutable and wired to
``S3Bucket.regional_domain_name``, which is unknown at plan time, so any change
to the bucket plans the distribution as a conditional ("known after apply")
REPLACE. The apply confirms it against the bucket's real outputs: a tag-only
update leaves the domain alone, so the distribution keeps its id and domain; a
renamed bucket moves the domain, so it is replaced.
"""

from __future__ import annotations

import boto3
import pytest

from atlantide.cli.views.report import report_json
from atlantide.reconcile import Action
from tests.providers.aws.conftest import mixed_engine

pytestmark = pytest.mark.usefixtures("aws_env")

BUCKET = "default:aws.S3Bucket:origin"
CDN = "default:aws.CloudFrontDistribution:cdn"
POLICY = "default:aws.S3BucketPolicy:policy"


def _site(bucket: str = "atlantide-cond-site", tags: str = "{'v': '1'}") -> str:
    return (
        "from atlantide.providers.aws import (S3Bucket, OriginAccessControl, "
        "CloudFrontDistribution, S3BucketPolicy, ServicePrincipal, allow)\n"
        f"b = S3Bucket('origin', bucket={bucket!r}, tags={tags})\n"
        "oac = OriginAccessControl('oac', oac_name='cond-oac')\n"
        "cdn = CloudFrontDistribution('cdn', origin_domain=b.regional_domain_name, "
        "oac_id=oac.oac_id)\n"
        "S3BucketPolicy('policy', bucket=b.bucket, statements=[allow('s3:GetObject', "
        "on=b.objects_arn, principal={'Service': ServicePrincipal.CloudFront}, "
        "condition={'StringEquals': {'AWS:SourceArn': cdn.arn}})])\n"
    )


def _distributions() -> list[str]:
    listing = boto3.client("cloudfront").list_distributions()["DistributionList"]
    return [item["Id"] for item in listing.get("Items", [])]


async def test_a_tag_only_bucket_update_keeps_the_distribution() -> None:
    engine = mixed_engine()
    (await engine.apply(_site())).unwrap()
    before = engine.backend.load().nodes[CDN].outputs["distribution_id"]

    retagged = _site(tags="{'v': '2'}")
    planned = {c.node_id: c for c in engine.plan(retagged).unwrap().changeset}
    assert planned[BUCKET].action is Action.UPDATE
    assert (planned[CDN].action, planned[CDN].conditional) == (Action.REPLACE, True)

    report = (await engine.apply(retagged)).unwrap()

    assert CDN not in report.replaced and CDN not in report.created
    assert report.downgraded[CDN] == "noop"
    assert _distributions() == [before]
    assert engine.backend.load().nodes[CDN].outputs["distribution_id"] == before
    as_json = report_json(report)
    assert CDN in as_json["noop"] and CDN not in as_json["replaced"]
    assert as_json["downgraded"][CDN] == "noop"
    # And the next plan has nothing left to do.
    again = engine.plan(retagged).unwrap().changeset
    assert again.pending == []


async def test_a_renamed_bucket_still_replaces_the_distribution() -> None:
    engine = mixed_engine()
    (await engine.apply(_site())).unwrap()
    before = engine.backend.load().nodes[CDN].outputs["distribution_id"]

    report = (await engine.apply(_site(bucket="atlantide-cond-site-2"))).unwrap()

    assert BUCKET in report.replaced
    assert CDN in report.replaced
    assert CDN not in report.downgraded
    after = engine.backend.load().nodes[CDN].outputs["distribution_id"]
    assert after != before
    assert _distributions() == [after]
    assert engine.plan(_site(bucket="atlantide-cond-site-2")).unwrap().changeset.pending == []
