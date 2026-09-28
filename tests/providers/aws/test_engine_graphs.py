"""Cross-service behaviour: dispatch, name-keyed adoption, reads of absent
resources, and whole AWS (and mixed local + AWS) graphs through the engine."""

from __future__ import annotations

import json
from pathlib import Path

import boto3
import pytest

from atlantide.core import Context
from atlantide.core.errors import ProviderError
from atlantide.core.resource import Resource
from atlantide.providers.aws import (
    AcmCertificate,
    AwsProvider,
    CloudFrontDistribution,
    CloudWatchLogGroup,
    DynamoDbTable,
    IamRole,
    LambdaFunction,
    OriginAccessControl,
    Route53HostedZone,
    Route53Record,
    S3BucketPolicy,
    SecurityGroup,
    ServicePrincipal,
    Subnet,
    Vpc,
    allow,
)
from tests.providers.aws.conftest import TRUST_POLICY, exists, mixed_engine

pytestmark = pytest.mark.usefixtures("aws_env")


# -- dispatch --------------------------------------------------------------------


async def test_unknown_resource_type_errors() -> None:
    class Foreign(Resource):
        class Meta:
            provider = "aws"

    provider = AwsProvider()
    with pytest.raises(ProviderError, match="cannot create"):
        await provider.create(Context(), Foreign("x"))


# -- mixed-provider graph through the engine -------------------------------------


async def test_mixed_local_and_aws_graph(tmp_path: Path) -> None:
    engine = mixed_engine()
    rec = tmp_path / "rec.txt"
    config = (
        "from atlantide.providers.aws import S3Bucket\n"
        "from atlantide.providers.local import File\n"
        "b = S3Bucket('logs', bucket='mixed-logs')\n"
        f"File('rec', path={str(rec)!r}, content=b.arn)\n"
    )

    report = (await engine.apply(config)).unwrap()
    assert len(report.created) == 2
    # AWS bucket exists; local file recorded the bucket's (cross-provider) arn
    assert exists("mixed-logs")
    assert rec.read_text() == "arn:aws:s3:::mixed-logs"

    # re-apply -> NOOP for both providers
    report2 = (await engine.apply(config)).unwrap()
    assert len(report2.noop) == 2

    # immutable bucket rename -> REPLACE
    renamed = config.replace("bucket='mixed-logs'", "bucket='mixed-logs-2'")
    report3 = (await engine.apply(renamed)).unwrap()
    assert "default:aws.S3Bucket:logs" in report3.replaced
    assert exists("mixed-logs-2") and not exists("mixed-logs")


async def test_all_three_aws_resources_in_one_apply() -> None:
    engine = mixed_engine()
    config = (
        "from atlantide.providers.aws import S3Bucket, SqsQueue, IamRole\n"
        "S3Bucket('bucket', bucket='multi-bucket')\n"
        "SqsQueue('queue', queue_name='multi-queue')\n"
        "IamRole('role', role_name='multi-role',"
        f" assume_role_policy={TRUST_POLICY!r})\n"
    )

    report = (await engine.apply(config)).unwrap()
    assert len(report.created) == 3
    assert exists("multi-bucket")
    assert boto3.client("sqs").get_queue_url(QueueName="multi-queue")["QueueUrl"]
    assert boto3.client("iam").get_role(RoleName="multi-role")["Role"]["RoleName"] == "multi-role"

    # re-apply -> all NOOP (Merkle skip across every service)
    assert len((await engine.apply(config)).unwrap().noop) == 3

    assert len((await engine.destroy()).unwrap().deleted) == 3


async def test_iam_policy_with_queue_ref_through_engine() -> None:
    # A policy whose statement references the queue's (computed) arn: the engine
    # must order role+queue before the policy and resolve the Ref before writing.
    engine = mixed_engine()
    config = (
        "from atlantide.providers.aws import S3Bucket, IamRole, SqsQueue, IamPolicy, allow\n"
        f"r = IamRole('role', role_name='pol-role', assume_role_policy={TRUST_POLICY!r})\n"
        "q = SqsQueue('queue', queue_name='pol-queue')\n"
        "b = S3Bucket('bucket', bucket='pol-bucket')\n"
        "IamPolicy('pol', role_arn=r.arn, policy_name='send',\n"
        "          statements=[allow('sqs:SendMessage', on=q.arn),\n"
        "                      allow('s3:GetObject', on=b.objects_arn)])\n"
    )

    assert len((await engine.apply(config)).unwrap().created) == 4

    doc = boto3.client("iam").get_role_policy(RoleName="pol-role", PolicyName="send")
    statements = doc["PolicyDocument"]["Statement"]
    # both computed Refs resolved: queue arn and the bucket's <arn>/* objects arn
    assert statements[0]["Resource"].endswith(":pol-queue")
    assert statements[1]["Resource"] == "arn:aws:s3:::pol-bucket/*"

    # re-apply -> NOOP (structured statements hash stably)
    assert len((await engine.apply(config)).unwrap().noop) == 4


# -- create idempotency, keyed on a name -----------------------------------------


#: Built inside the test, not at collection time: a resource's region comes from
#: the active stack, which the `aws_env` fixture sets up.
#:
#: Route53 is deliberately absent — moto lets a repeated CallerReference create a
#: second zone, where real Route53 raises HostedZoneAlreadyExists, so a test here
#: would be asserting moto's behaviour rather than the handler's.
_NAMED = {
    "iam_role": lambda: IamRole("r", role_name="atl-role", assumed_by=ServicePrincipal.Lambda),
    "dynamodb_table": lambda: DynamoDbTable("t", table_name="atl-table", hash_key="pk"),
}


@pytest.mark.parametrize("kind", sorted(_NAMED))
async def test_named_create_adopts_instead_of_erroring(kind: str) -> None:
    """A second create raises AlreadyExists/Conflict; adoption keyed on the name
    resolves to the resource this node declares."""
    provider = AwsProvider()
    resource = _NAMED[kind]()
    first = await provider.create(Context(), resource)
    assert await provider.create(Context(), resource) == first


# -- reads of absent resources, and engine graphs --------------------------------


async def test_new_resources_read_missing_is_none() -> None:
    provider = AwsProvider()
    ctx = Context()
    assert (
        await provider.read(
            ctx, LambdaFunction("f", function_name="ghost", role_arn="arn:aws:iam::0:role/x")
        )
        is None
    )
    assert await provider.read(ctx, DynamoDbTable("d", table_name="ghost", hash_key="id")) is None
    assert await provider.read(ctx, CloudWatchLogGroup("l", log_group_name="/ghost")) is None
    assert await provider.read(ctx, Vpc("v", cidr_block="192.168.0.0/16")) is None
    assert (
        await provider.read(ctx, Subnet("s", vpc_id="vpc-ghost", cidr_block="192.168.1.0/24"))
        is None
    )
    assert (
        await provider.read(ctx, SecurityGroup("g", group_name="ghost", vpc_id="vpc-ghost")) is None
    )
    assert (
        await provider.read(
            ctx,
            S3BucketPolicy(
                "p",
                bucket="ghost-bucket",
                statements=[allow("s3:GetObject", on="x", principal="*")],
            ),
        )
        is None
    )
    assert await provider.read(ctx, OriginAccessControl("o", oac_name="ghost")) is None
    assert (
        await provider.read(
            ctx, CloudFrontDistribution("c", origin_domain="ghost.s3.amazonaws.com", oac_id="ghost")
        )
        is None
    )
    assert await provider.read(ctx, AcmCertificate("a", domain_name="ghost.example.com")) is None
    assert await provider.read(ctx, Route53HostedZone("z", domain="ghost.example.com")) is None
    assert (
        await provider.read(
            ctx,
            Route53Record(
                "r",
                zone_id="Zghost",
                record_name="www.ghost.example.com",
                record_type="CNAME",
                records=["x"],
            ),
        )
        is None
    )


async def test_networking_chain_through_engine() -> None:
    # vpc_id Refs force ordering: vpc before subnet+sg on apply, reverse on destroy.
    engine = mixed_engine()
    config = (
        "from atlantide.providers.aws import Vpc, Subnet, SecurityGroup\n"
        "v = Vpc('vpc', cidr_block='10.0.0.0/16')\n"
        "Subnet('subnet', vpc_id=v.vpc_id, cidr_block='10.0.1.0/24')\n"
        "SecurityGroup('sg', group_name='web', vpc_id=v.vpc_id)\n"
    )

    assert len((await engine.apply(config)).unwrap().created) == 3
    # the subnet was created inside the vpc (its Ref resolved to the real vpc id)
    subnets = boto3.client("ec2").describe_subnets(
        Filters=[{"Name": "cidr-block", "Values": ["10.0.1.0/24"]}]
    )["Subnets"]
    vpcs = boto3.client("ec2").describe_vpcs(Filters=[{"Name": "cidr", "Values": ["10.0.0.0/16"]}])[
        "Vpcs"
    ]
    assert subnets[0]["VpcId"] == vpcs[0]["VpcId"]

    # re-apply -> NOOP, destroy removes all three (dependents first)
    assert len((await engine.apply(config)).unwrap().noop) == 3
    assert len((await engine.destroy()).unwrap().deleted) == 3


async def test_static_site_graph_through_engine() -> None:
    # bucket + OAC -> distribution -> bucket policy; the policy's OAC condition
    # references the distribution arn, resolved before the policy is written.
    engine = mixed_engine()
    config = (
        "from atlantide.providers.aws import (S3Bucket, OriginAccessControl, "
        "CloudFrontDistribution, S3BucketPolicy, ServicePrincipal, allow)\n"
        "b = S3Bucket('origin', bucket='atlantide-site-test')\n"
        "oac = OriginAccessControl('oac', oac_name='site-oac')\n"
        "cdn = CloudFrontDistribution('cdn', origin_domain=b.regional_domain_name, "
        "oac_id=oac.oac_id)\n"
        "S3BucketPolicy('policy', bucket=b.bucket, statements=[allow('s3:GetObject', "
        "on=b.objects_arn, principal={'Service': ServicePrincipal.CloudFront}, "
        "condition={'StringEquals': {'AWS:SourceArn': cdn.arn}})])\n"
    )
    assert len((await engine.apply(config)).unwrap().created) == 4
    doc = json.loads(boto3.client("s3").get_bucket_policy(Bucket="atlantide-site-test")["Policy"])
    source_arn = doc["Statement"][0]["Condition"]["StringEquals"]["AWS:SourceArn"]
    assert source_arn.startswith("arn:aws:cloudfront:")  # the real distribution arn
    # re-apply -> NOOP; destroy removes all four (exercises CloudFront disable-then-delete)
    assert len((await engine.apply(config)).unwrap().noop) == 4
    assert len((await engine.destroy()).unwrap().deleted) == 4
