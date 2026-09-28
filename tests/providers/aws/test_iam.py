"""IAM roles and inline policies, plus the policy and principal builders."""

from __future__ import annotations

import json

import boto3
import pytest

from atlantide.core import Context, Stack
from atlantide.providers.aws import (
    AwsProvider,
    IamPolicy,
    IamRole,
    Region,
    S3Bucket,
    ServicePrincipal,
    SqsQueue,
    allow,
    deny,
)
from tests.providers.aws.conftest import TRUST_POLICY

pytestmark = pytest.mark.usefixtures("aws_env")


async def test_iam_create_read_update_delete() -> None:
    provider = AwsProvider()
    res = IamRole("r", role_name="svc", assume_role_policy=TRUST_POLICY, description="hi")
    out = await provider.create(Context(), res)
    assert out["arn"].endswith(":role/svc")

    assert await provider.read(Context(), res) is not None

    await provider.update(
        Context(),
        out,
        IamRole("r", role_name="svc", assume_role_policy=TRUST_POLICY, description="changed"),
    )
    role = boto3.client("iam").get_role(RoleName="svc")["Role"]
    assert role["Description"] == "changed"

    await provider.delete(Context(), res)
    assert await provider.read(Context(), res) is None


async def test_iam_role_assumed_by_builds_trust_policy() -> None:
    provider = AwsProvider()
    res = IamRole("r", role_name="svc", assumed_by="lambda.amazonaws.com")
    await provider.create(Context(), res)
    doc = boto3.client("iam").get_role(RoleName="svc")["Role"]["AssumeRolePolicyDocument"]
    assert doc["Statement"][0]["Principal"]["Service"] == "lambda.amazonaws.com"


def test_iam_role_trust_source_is_exclusive() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        IamRole("r", role_name="svc")  # neither assumed_by nor assume_role_policy
    with pytest.raises(ValueError, match="exactly one"):
        IamRole(
            "r", role_name="svc", assumed_by="ec2.amazonaws.com", assume_role_policy=TRUST_POLICY
        )  # both


def test_region_constants() -> None:
    assert Region.UsEast1 == "us-east-1"
    assert Region.EuNorth1 == "eu-north-1"
    # usable directly as a stack/resource region
    with Stack("t", region=Region.UsWest2):
        assert S3Bucket("b", bucket="rgn-bucket").region == "us-west-2"


def test_service_principal_constants() -> None:
    from atlantide.providers.aws import ServicePrincipal

    assert ServicePrincipal.Ec2 == "ec2.amazonaws.com"
    assert ServicePrincipal.Lambda == "lambda.amazonaws.com"
    role = IamRole("r", role_name="svc", assumed_by=ServicePrincipal.Lambda)
    assert role.assumed_by == "lambda.amazonaws.com"


def test_assume_role_builder() -> None:
    from atlantide.providers.aws import assume_role

    single = json.loads(assume_role("lambda.amazonaws.com"))
    assert single["Statement"][0]["Principal"]["Service"] == "lambda.amazonaws.com"
    multi = json.loads(assume_role("ec2.amazonaws.com", "lambda.amazonaws.com"))
    assert multi["Statement"][0]["Principal"]["Service"] == [
        "ec2.amazonaws.com",
        "lambda.amazonaws.com",
    ]
    with pytest.raises(ValueError, match="at least one service"):
        assume_role()


async def test_iam_read_missing_is_none() -> None:
    provider = AwsProvider()
    res = IamRole("r", role_name="ghost", assume_role_policy=TRUST_POLICY)
    assert await provider.read(Context(), res) is None


_S3_STATEMENTS = [allow("s3:GetObject", "s3:PutObject", on="arn:aws:s3:::assets/*")]


async def test_iam_policy_create_read_update_delete() -> None:
    provider = AwsProvider()
    role = IamRole("r", role_name="worker", assume_role_policy=TRUST_POLICY)
    role_out = await provider.create(Context(), role)

    pol = IamPolicy("p", role_arn=role_out["arn"], policy_name="s3", statements=_S3_STATEMENTS)
    assert await provider.create(Context(), pol) == {}
    assert await provider.read(Context(), pol) is not None

    # the statements were serialized into a valid IAM policy document
    doc = boto3.client("iam").get_role_policy(RoleName="worker", PolicyName="s3")
    assert doc["PolicyName"] == "s3"
    actions = doc["PolicyDocument"]["Statement"][0]["Action"]
    assert actions == ["s3:GetObject", "s3:PutObject"]

    await provider.update(Context(), {}, pol)
    await provider.delete(Context(), pol)
    assert await provider.read(Context(), pol) is None


async def test_iam_policy_read_missing_is_none() -> None:
    provider = AwsProvider()
    pol = IamPolicy(
        "p",
        role_arn="arn:aws:iam::123456789012:role/ghost",
        policy_name="s3",
        statements=_S3_STATEMENTS,
    )
    assert await provider.read(Context(), pol) is None


def test_action_constants() -> None:
    # plain str constants, not model fields, usable directly in allow()
    assert S3Bucket.Action.GetObject == "s3:GetObject"
    assert SqsQueue.Action.SendMessage == "sqs:SendMessage"
    assert "Action" not in S3Bucket.model_fields
    assert allow(S3Bucket.Action.ListBucket, on="a")["Action"] == ["s3:ListBucket"]


def test_policy_builders() -> None:
    assert allow("s3:GetObject", on="arn:aws:s3:::b/*") == {
        "Effect": "Allow",
        "Action": ["s3:GetObject"],
        "Resource": "arn:aws:s3:::b/*",
    }
    assert deny("s3:*", on=["a", "b"], sid="no")["Effect"] == "Deny"
    with pytest.raises(ValueError, match="at least one action"):
        allow(on="arn:aws:s3:::b")


def test_policy_builder_condition_and_service_principal() -> None:
    assert ServicePrincipal.CloudFront == "cloudfront.amazonaws.com"
    statement = allow(
        "s3:GetObject",
        on="arn:aws:s3:::b/*",
        principal={"Service": ServicePrincipal.CloudFront},
        condition={"StringEquals": {"AWS:SourceArn": "arn:aws:cloudfront::0:distribution/X"}},
    )
    assert statement["Principal"] == {"Service": "cloudfront.amazonaws.com"}
    assert statement["Condition"] == {
        "StringEquals": {"AWS:SourceArn": "arn:aws:cloudfront::0:distribution/X"}
    }
    # no condition -> no Condition key
    assert "Condition" not in allow("s3:GetObject", on="x")
