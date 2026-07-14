"""The aws.SecureBucket L2 component applies end-to-end through the engine."""

from __future__ import annotations

import json

import boto3
from moto import mock_aws

from atlantide.core import Stack
from atlantide.core.resource import collecting
from atlantide.providers import aws
from atlantide.providers.aws import AwsProvider, SecureBucket
from atlantide.reconcile import Action
from tests.conftest import make_engine
from tests.support import cloud_env_fixture

aws_env = cloud_env_fixture(
    {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    },
    region="us-east-1",
    mock_factory=mock_aws,
)

_SITE = (
    "from atlantide.providers.aws import SecureBucket\n"
    "SecureBucket('web', bucket='atlantide-secure-bucket')\n"
)


def test_component_expands_to_flat_nodes() -> None:
    with collecting() as reg, Stack("default", region="us-east-1"):
        SecureBucket("web", bucket="atlantide-secure-bucket")
    ids = sorted(r.node_id for r in reg.all())
    assert ids == [
        "default:aws.S3Bucket:web-assets",
        "default:aws.S3BucketPolicy:web-policy",
    ]


async def test_secure_bucket_applies() -> None:
    engine = make_engine(aws.TYPES, AwsProvider())

    # Plan on empty state: both children CREATE, policy ordered after the bucket.
    planned = engine.plan(_SITE).unwrap()
    assert {c.action for c in planned.changeset} == {Action.CREATE}

    (await engine.apply(_SITE)).unwrap()
    names = {b["Name"] for b in boto3.client("s3").list_buckets()["Buckets"]}
    assert "atlantide-secure-bucket" in names

    # The policy is a TLS-only Deny (no public grant) referencing the bucket's ARNs.
    raw = boto3.client("s3").get_bucket_policy(Bucket="atlantide-secure-bucket")["Policy"]
    statement = json.loads(raw)["Statement"][0]
    assert statement["Effect"] == "Deny"
    assert statement["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
    assert "arn:aws:s3:::atlantide-secure-bucket" in statement["Resource"]

    # Re-apply unchanged -> all NOOP.
    report = (await engine.apply(_SITE)).unwrap()
    assert len(report.noop) == 2
