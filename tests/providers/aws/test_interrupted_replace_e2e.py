"""A bucket rename interrupted before the CloudFront distribution in front of it.

Renaming the origin bucket replaces it, which moves ``regional_domain_name``;
``CloudFrontDistribution.origin_domain`` is immutable and wired to it. An apply
stopped after the bucket's replace but before the distribution leaves the
distribution's row, marker for marker, equal to the config while the
distribution still points at the old bucket. The next plan must replace it
(known, not conditional) rather than push the new domain through ``update``.

The bucket is create-before-destroy: a destroy-first bucket takes the
distribution down before its own delete (phase 0), so no interruption leaves a
distribution behind it untouched.
"""

from __future__ import annotations

import asyncio
from typing import Any

import boto3
import pytest

from atlantide.engine import Engine
from atlantide.reconcile import Action
from atlantide.reconcile.executor import run as run_module
from tests.providers.aws.conftest import mixed_engine

pytestmark = pytest.mark.usefixtures("aws_env")

BUCKET = "default:aws.S3Bucket:origin"
CDN = "default:aws.CloudFrontDistribution:cdn"
POLICY = "default:aws.S3BucketPolicy:policy"


def _site(bucket: str = "atlantide-int-site") -> str:
    return (
        "from atlantide.providers.aws import (S3Bucket, OriginAccessControl, "
        "CloudFrontDistribution, S3BucketPolicy, ServicePrincipal, allow)\n"
        "from atlantide.core import Lifecycle\n"
        f"b = S3Bucket('origin', bucket={bucket!r}, "
        "lifecycle=Lifecycle(create_before_destroy=True))\n"
        "oac = OriginAccessControl('oac', oac_name='int-oac')\n"
        "cdn = CloudFrontDistribution('cdn', origin_domain=b.regional_domain_name, "
        "oac_id=oac.oac_id)\n"
        "S3BucketPolicy('policy', bucket=b.bucket, statements=[allow('s3:GetObject', "
        "on=b.objects_arn, principal={'Service': ServicePrincipal.CloudFront}, "
        "condition={'StringEquals': {'AWS:SourceArn': cdn.arn}})])\n"
    )


def _distributions() -> list[str]:
    listing = boto3.client("cloudfront").list_distributions()["DistributionList"]
    return [item["Id"] for item in listing.get("Items", [])]


def _origin(distribution_id: str) -> str:
    config = boto3.client("cloudfront").get_distribution_config(Id=distribution_id)
    return str(config["DistributionConfig"]["Origins"]["Items"][0]["DomainName"])


async def _interrupted(engine: Engine, source: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply ``source``, cancelling it once the bucket is replaced and the
    distribution is about to start."""
    original = run_module.ChangeSetRun._apply_node
    reached = asyncio.Event()

    async def parked(self: Any, node_id: str) -> None:
        if node_id in (CDN, POLICY):
            reached.set()
            await asyncio.sleep(3600)  # cancelled from outside
        await original(self, node_id)

    monkeypatch.setattr(run_module.ChangeSetRun, "_apply_node", parked)
    task = asyncio.ensure_future(engine.apply(source, on_failure="halt"))
    await asyncio.wait_for(reached.wait(), timeout=30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.undo()


async def test_an_interrupted_bucket_rename_replaces_the_distribution_next_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = mixed_engine()
    (await engine.apply(_site())).unwrap()
    before = engine.backend.load().nodes[CDN].outputs["distribution_id"]
    old_domain = _origin(before)

    renamed = _site(bucket="atlantide-int-site-2")
    await _interrupted(engine, renamed, monkeypatch)
    rows = engine.backend.load().nodes
    assert rows[BUCKET].properties["bucket"] == "atlantide-int-site-2"
    assert rows[CDN].outputs["distribution_id"] == before  # never reached

    planned = {c.node_id: c for c in engine.plan(renamed).unwrap().changeset}
    assert planned[BUCKET].action is Action.NOOP
    assert planned[f"{BUCKET}~replaced"].action is Action.DELETE  # the old bucket
    assert planned[CDN].action is Action.REPLACE
    assert "origin_domain" in planned[CDN].changed_fields
    assert "origin_domain" in planned[CDN].upstream_moved
    assert planned[CDN].conditional is False

    report = (await engine.apply(renamed)).unwrap()

    assert CDN in report.replaced and CDN not in report.updated
    after = engine.backend.load().nodes[CDN].outputs["distribution_id"]
    assert after != before
    assert _distributions() == [after]
    assert _origin(after) != old_domain
    assert _origin(after) == engine.backend.load().nodes[BUCKET].outputs["regional_domain_name"]
    assert engine.plan(renamed).unwrap().changeset.pending == []
