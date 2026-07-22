"""AWS S3Bucket provider under moto + a mixed local+aws graph through the engine."""

from __future__ import annotations

import json
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from atlantide.core import Context, Stack
from atlantide.core.errors import LanguageError, ProviderError
from atlantide.core.resource import Resource
from atlantide.engine import Engine
from atlantide.providers import aws, local
from atlantide.providers.aws import (
    AcmCertificate,
    AwsProvider,
    CloudFrontDistribution,
    CloudWatchLogGroup,
    DynamoDbTable,
    IamPolicy,
    IamRole,
    LambdaFunction,
    OriginAccessControl,
    Region,
    Route53HostedZone,
    Route53Record,
    S3Bucket,
    S3BucketPolicy,
    S3Folder,
    SecurityGroup,
    ServicePrincipal,
    SnsSubscription,
    SnsTopic,
    SqsQueue,
    Subnet,
    Vpc,
    allow,
    deny,
)
from atlantide.providers.local import LocalProvider
from tests.conftest import make_engine
from tests.support import cloud_env_fixture

_TRUST_POLICY = (
    '{"Version": "2012-10-17", "Statement": [{"Effect": "Allow",'
    ' "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]}'
)

# Autouse: moto + AWS creds + a "default" stack (resources require a region; the
# stack supplies it and keeps the node-id prefix "default"). The cloud-test kit
# makes this reusable — a second provider swaps env + mock_factory.
aws_env = cloud_env_fixture(
    {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    },
    region="us-east-1",
    mock_factory=mock_aws,
)


def _exists(bucket: str) -> bool:
    names = {b["Name"] for b in boto3.client("s3").list_buckets()["Buckets"]}
    return bucket in names


async def test_create_bucket_and_outputs() -> None:
    provider = AwsProvider()
    out = await provider.create(Context(), S3Bucket("b", bucket="my-logs"))
    assert out == {
        "name": "my-logs",
        "arn": "arn:aws:s3:::my-logs",
        "objects_arn": "arn:aws:s3:::my-logs/*",
        "bucket": "my-logs",
        "regional_domain_name": "my-logs.s3.us-east-1.amazonaws.com",
    }
    assert _exists("my-logs")


async def test_versioning_and_tags() -> None:
    provider = AwsProvider()
    await provider.create(
        Context(),
        S3Bucket("b", bucket="ver", versioning=True, tags={"env": "prod"}),
    )
    client = boto3.client("s3")
    assert client.get_bucket_versioning(Bucket="ver")["Status"] == "Enabled"
    tags = {t["Key"]: t["Value"] for t in client.get_bucket_tagging(Bucket="ver")["TagSet"]}
    assert tags == {"env": "prod"}


async def test_update_tags() -> None:
    provider = AwsProvider()
    res = S3Bucket("b", bucket="upd", tags={"a": "1"})
    await provider.create(Context(), res)
    await provider.update(Context(), {}, S3Bucket("b", bucket="upd", tags={"a": "2", "b": "3"}))
    client = boto3.client("s3")
    tags = {t["Key"]: t["Value"] for t in client.get_bucket_tagging(Bucket="upd")["TagSet"]}
    assert tags == {"a": "2", "b": "3"}


async def test_create_regional_bucket() -> None:
    # region != us-east-1 needs a matching client + LocationConstraint.
    provider = AwsProvider()
    out = await provider.create(Context(), S3Bucket("b", bucket="eu-bucket", region="eu-north-1"))
    assert out["bucket"] == "eu-bucket"
    client = boto3.client("s3", region_name="eu-north-1")
    loc = client.get_bucket_location(Bucket="eu-bucket")["LocationConstraint"]
    assert loc == "eu-north-1"


async def test_create_is_idempotent_when_already_owned() -> None:
    provider = AwsProvider()
    res = S3Bucket("b", bucket="owned-twice")
    await provider.create(Context(), res)
    # a second create (e.g. resuming a partial apply) must not error
    out = await provider.create(Context(), res)
    assert out["bucket"] == "owned-twice"


async def test_read_missing_is_none() -> None:
    provider = AwsProvider()
    assert await provider.read(Context(), S3Bucket("b", bucket="ghost")) is None


async def test_delete_bucket() -> None:
    provider = AwsProvider()
    res = S3Bucket("b", bucket="gone")
    await provider.create(Context(), res)
    assert _exists("gone")
    await provider.delete(Context(), res)
    assert not _exists("gone")


# -- S3Folder ----------------------------------------------------------------


def _site(root: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return root


def _objects(bucket: str, prefix: str = "") -> dict[str, str]:
    client = boto3.client("s3")
    resp = client.list_objects_v2(Bucket=bucket, Prefix=prefix)
    return {
        obj["Key"]: client.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read().decode()
        for obj in resp.get("Contents", [])
    }


def test_s3folder_manifest_is_deterministic_and_excludes_caches(tmp_path: Path) -> None:
    root = _site(tmp_path, {"index.html": "hi", "css/app.css": "body{}"})
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "x.pyc").write_text("junk")

    folder = S3Folder("assets", bucket="b", source_path=str(root))
    assert set(folder.manifest) == {"index.html", "css/app.css"}  # posix rels, no caches
    # Re-reading the same tree yields an identical manifest.
    assert S3Folder("a2", bucket="b", source_path=str(root)).manifest == folder.manifest


def test_s3folder_source_path_must_be_literal() -> None:
    ref_path = S3Bucket("b", bucket="some-bucket").regional_domain_name  # a Ref
    with pytest.raises(LanguageError, match="literal directory"):
        S3Folder("assets", bucket="b", source_path=ref_path)


def test_s3folder_pinned_manifest_skips_disk() -> None:
    # A rehydrate (deploy) passes a pinned manifest and must not touch disk.
    folder = S3Folder(
        "assets", bucket="b", source_path="/does/not/exist", manifest={"a.txt": "abc123"}
    )
    assert folder.manifest == {"a.txt": "abc123"}


def test_s3folder_bucket_name_creates_dependency_edge(tmp_path: Path) -> None:
    from atlantide.core import collecting
    from atlantide.ir import lower

    root = _site(tmp_path, {"index.html": "hi"})
    with collecting() as reg, Stack("s", region=Region.UsEast1):
        b = S3Bucket("site", bucket="dep-site")
        folder = S3Folder("assets", bucket=b.name, source_path=str(root))
    node = lower(reg).node(folder.node_id)
    assert node is not None
    assert b.node_id in node.dependencies  # b.name (a Ref) orders folder after bucket


async def test_s3folder_create_uploads_all(tmp_path: Path) -> None:
    root = _site(tmp_path, {"index.html": "<h1>hi</h1>", "css/app.css": "body{}"})
    provider = AwsProvider()
    boto3.client("s3").create_bucket(Bucket="site")

    out = await provider.create(
        Context(), S3Folder("assets", bucket="site", source_path=str(root), prefix="web/")
    )
    assert set(out["uploaded"]) == {"web/index.html", "web/css/app.css"}
    assert _objects("site") == {"web/index.html": "<h1>hi</h1>", "web/css/app.css": "body{}"}
    # Content-Type is inferred from the key's extension.
    head = boto3.client("s3").head_object(Bucket="site", Key="web/index.html")
    assert head["ContentType"] == "text/html"


async def test_s3folder_update_syncs_delta_and_prunes(tmp_path: Path) -> None:
    provider = AwsProvider()
    boto3.client("s3").create_bucket(Bucket="site")
    root = _site(tmp_path, {"index.html": "v1", "app.css": "body{}", "old.txt": "bye"})
    prior = await provider.create(
        Context(), S3Folder("assets", bucket="site", source_path=str(root), prefix="web/")
    )

    # Change index.html, add main.js, remove old.txt.
    (root / "index.html").write_text("v2")
    (root / "main.js").write_text("console.log(1)")
    (root / "old.txt").unlink()
    updated = S3Folder("assets", bucket="site", source_path=str(root), prefix="web/")

    out = await provider.update(Context(), prior, updated)
    assert set(out["uploaded"]) == {"web/index.html", "web/app.css", "web/main.js"}
    assert _objects("site") == {
        "web/index.html": "v2",
        "web/app.css": "body{}",
        "web/main.js": "console.log(1)",
    }  # old.txt pruned


async def test_s3folder_delete_removes_objects(tmp_path: Path) -> None:
    provider = AwsProvider()
    boto3.client("s3").create_bucket(Bucket="site")
    root = _site(tmp_path, {"index.html": "hi", "a.css": "x"})
    res = S3Folder("assets", bucket="site", source_path=str(root), prefix="web/")
    out = await provider.create(Context(), res)

    # State restores the computed ``uploaded`` map onto the resource for delete.
    res.uploaded = out["uploaded"]  # type: ignore[misc]
    await provider.delete(Context(), res)
    assert _objects("site") == {}


async def test_s3folder_read_missing_bucket_is_none() -> None:
    provider = AwsProvider()
    res = S3Folder("assets", bucket="ghost", source_path="/tmp", manifest={})
    assert await provider.read(Context(), res) is None


async def test_s3folder_through_engine_noop_update_replace(tmp_path: Path) -> None:
    engine = _mixed_engine()
    root = _site(tmp_path, {"index.html": "v1"})
    config = (
        "from atlantide.providers.aws import S3Bucket, S3Folder\n"
        "b = S3Bucket('site', bucket='eng-site')\n"
        # bucket=b.name orders the folder after the bucket (a literal name would not).
        f"S3Folder('assets', bucket=b.name, source_path={str(root)!r}, prefix='web/')\n"
    )

    report = (await engine.apply(config)).unwrap()
    assert len(report.created) == 2
    assert _objects("eng-site") == {"web/index.html": "v1"}

    # Re-apply unchanged -> Merkle NOOP for both nodes.
    report2 = (await engine.apply(config)).unwrap()
    assert len(report2.noop) == 2

    # Edit a file on disk -> manifest changes -> S3Folder UPDATE (bucket unchanged).
    (root / "index.html").write_text("v2")
    report3 = (await engine.apply(config)).unwrap()
    assert "default:aws.S3Folder:assets" in report3.updated
    assert _objects("eng-site") == {"web/index.html": "v2"}

    # Change the immutable prefix -> REPLACE.
    replaced = config.replace("prefix='web/'", "prefix='static/'")
    report4 = (await engine.apply(replaced)).unwrap()
    assert "default:aws.S3Folder:assets" in report4.replaced


# -- SQS ---------------------------------------------------------------------


async def test_sqs_create_read_update_delete() -> None:
    provider = AwsProvider()
    res = SqsQueue("q", queue_name="jobs", tags={"team": "infra"})
    out = await provider.create(Context(), res)
    assert out["url"].endswith("/jobs")
    assert out["arn"].endswith(":jobs")

    assert await provider.read(Context(), res) is not None

    await provider.update(Context(), out, SqsQueue("q", queue_name="jobs", tags={"team": "ops"}))
    client = boto3.client("sqs")
    tags = client.list_queue_tags(QueueUrl=out["url"]).get("Tags", {})
    assert tags["team"] == "ops"

    await provider.delete(Context(), res)
    assert await provider.read(Context(), res) is None


async def test_sqs_fifo_queue() -> None:
    provider = AwsProvider()
    out = await provider.create(Context(), SqsQueue("q", queue_name="events.fifo", fifo=True))
    attrs = boto3.client("sqs").get_queue_attributes(
        QueueUrl=out["url"], AttributeNames=["FifoQueue"]
    )
    assert attrs["Attributes"]["FifoQueue"] == "true"


async def test_sqs_fifo_name_gets_suffix() -> None:
    # AWS requires FIFO names to end in .fifo; the provider appends it, and
    # read/delete look the queue up under the same suffixed name.
    provider = AwsProvider()
    res = SqsQueue("q", queue_name="events", fifo=True)  # no .fifo suffix
    out = await provider.create(Context(), res)
    assert out["url"].endswith("/events.fifo")
    assert await provider.read(Context(), res) is not None  # found under events.fifo
    await provider.delete(Context(), res)
    assert await provider.read(Context(), res) is None


async def test_sqs_read_missing_is_none() -> None:
    provider = AwsProvider()
    assert await provider.read(Context(), SqsQueue("q", queue_name="nope")) is None


# -- plan-time input validation ----------------------------------------------


def test_input_validation_rejects_bad_values() -> None:
    with pytest.raises(ValueError, match="S3 bucket name"):
        S3Bucket("b", bucket="Not_A_Valid_Bucket")  # uppercase + underscore
    with pytest.raises(ValueError, match="SQS queue name"):
        SqsQueue("q", queue_name="has spaces")
    with pytest.raises(ValueError, match="80-character"):
        SqsQueue("q", queue_name="x" * 81)
    with pytest.raises(ValueError, match="CIDR"):
        Vpc("v", cidr_block="10.0.0/16")  # malformed
    with pytest.raises(ValueError, match="CIDR"):
        Subnet("s", vpc_id="vpc-1", cidr_block="10.0.0.999/24")  # octet > 255
    with pytest.raises(ValueError, match="billing_mode"):
        DynamoDbTable("d", table_name="t", hash_key="id", billing_mode="NOPE")
    with pytest.raises(ValueError, match="64-character"):
        IamRole("r", role_name="x" * 65, assumed_by=ServicePrincipal.Ec2)


def test_valid_inputs_and_refs_pass_validation() -> None:
    # good literals construct fine
    S3Bucket("b", bucket="atlantide-assets-dev")
    SqsQueue("q", queue_name="jobs", fifo=True)  # .fifo appended by the provider, name valid
    Vpc("v", cidr_block="10.0.0.0/16")
    # a validated field still holding a Ref is skipped (value unknown until apply)
    queue = SqsQueue("qref", queue_name="q1")
    S3Bucket("b2", bucket=queue.arn)  # bucket=Ref(queue.arn) -> validation skipped, no error


# -- IAM ---------------------------------------------------------------------


async def test_iam_create_read_update_delete() -> None:
    provider = AwsProvider()
    res = IamRole("r", role_name="svc", assume_role_policy=_TRUST_POLICY, description="hi")
    out = await provider.create(Context(), res)
    assert out["arn"].endswith(":role/svc")

    assert await provider.read(Context(), res) is not None

    await provider.update(
        Context(),
        out,
        IamRole("r", role_name="svc", assume_role_policy=_TRUST_POLICY, description="changed"),
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
            "r", role_name="svc", assumed_by="ec2.amazonaws.com", assume_role_policy=_TRUST_POLICY
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
    res = IamRole("r", role_name="ghost", assume_role_policy=_TRUST_POLICY)
    assert await provider.read(Context(), res) is None


_S3_STATEMENTS = [allow("s3:GetObject", "s3:PutObject", on="arn:aws:s3:::assets/*")]


async def test_iam_policy_create_read_update_delete() -> None:
    provider = AwsProvider()
    role = IamRole("r", role_name="worker", assume_role_policy=_TRUST_POLICY)
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
    # no condition -> no Condition key (existing callers unchanged)
    assert "Condition" not in allow("s3:GetObject", on="x")


# -- dispatch ----------------------------------------------------------------


async def test_unknown_resource_type_errors() -> None:
    class Foreign(Resource):
        class Meta:
            provider = "aws"

    provider = AwsProvider()
    with pytest.raises(ProviderError, match="cannot create"):
        await provider.create(Context(), Foreign("x"))


# -- mixed-provider graph through the engine ---------------------------------


def _mixed_engine() -> Engine:
    return make_engine({**local.TYPES, **aws.TYPES}, LocalProvider(), AwsProvider())


async def test_mixed_local_and_aws_graph(tmp_path: Path) -> None:
    engine = _mixed_engine()
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
    assert _exists("mixed-logs")
    assert rec.read_text() == "arn:aws:s3:::mixed-logs"

    # re-apply -> NOOP for both providers
    report2 = (await engine.apply(config)).unwrap()
    assert len(report2.noop) == 2

    # immutable bucket rename -> REPLACE
    renamed = config.replace("bucket='mixed-logs'", "bucket='mixed-logs-2'")
    report3 = (await engine.apply(renamed)).unwrap()
    assert "default:aws.S3Bucket:logs" in report3.replaced
    assert _exists("mixed-logs-2") and not _exists("mixed-logs")


async def test_all_three_aws_resources_in_one_apply() -> None:
    engine = _mixed_engine()
    config = (
        "from atlantide.providers.aws import S3Bucket, SqsQueue, IamRole\n"
        "S3Bucket('bucket', bucket='multi-bucket')\n"
        "SqsQueue('queue', queue_name='multi-queue')\n"
        "IamRole('role', role_name='multi-role',"
        f" assume_role_policy={_TRUST_POLICY!r})\n"
    )

    report = (await engine.apply(config)).unwrap()
    assert len(report.created) == 3
    assert _exists("multi-bucket")
    assert boto3.client("sqs").get_queue_url(QueueName="multi-queue")["QueueUrl"]
    assert boto3.client("iam").get_role(RoleName="multi-role")["Role"]["RoleName"] == "multi-role"

    # re-apply -> all NOOP (Merkle skip across every service)
    assert len((await engine.apply(config)).unwrap().noop) == 3

    # destroy removes all three
    assert len((await engine.destroy()).unwrap().deleted) == 3


async def test_iam_policy_with_queue_ref_through_engine() -> None:
    # A policy whose statement references the queue's (computed) arn: the engine
    # must order role+queue before the policy and resolve the Ref before writing.
    engine = _mixed_engine()
    config = (
        "from atlantide.providers.aws import S3Bucket, IamRole, SqsQueue, IamPolicy, allow\n"
        f"r = IamRole('role', role_name='pol-role', assume_role_policy={_TRUST_POLICY!r})\n"
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


# -- Lambda / SNS / DynamoDB / Logs / S3 bucket policy -----------------------

_LAMBDA_TRUST = _TRUST_POLICY.replace("ec2.amazonaws.com", "lambda.amazonaws.com")


async def test_lambda_create_read_update_delete() -> None:
    provider = AwsProvider()
    role_out = await provider.create(
        Context(), IamRole("r", role_name="fn-role", assume_role_policy=_LAMBDA_TRUST)
    )
    res = LambdaFunction("f", function_name="fn", role_arn=role_out["arn"], tags={"env": "t"})
    out = await provider.create(Context(), res)
    assert out["arn"].endswith(":function:fn")
    assert await provider.read(Context(), res) is not None

    await provider.update(
        Context(),
        out,
        LambdaFunction("f", function_name="fn", role_arn=role_out["arn"], runtime="python3.11"),
    )
    cfg = boto3.client("lambda").get_function(FunctionName="fn")["Configuration"]
    assert cfg["Runtime"] == "python3.11"

    await provider.delete(Context(), res)
    assert await provider.read(Context(), res) is None


async def test_sns_topic_and_subscription() -> None:
    provider = AwsProvider()
    topic_out = await provider.create(Context(), SnsTopic("t", name="events", tags={"a": "1"}))
    assert topic_out["arn"].endswith(":events")
    queue_out = await provider.create(Context(), SqsQueue("q", queue_name="events-q"))

    sub = SnsSubscription("s", topic_arn=topic_out["arn"], endpoint=queue_out["arn"])
    sub_out = await provider.create(Context(), sub)
    assert sub_out["subscription_arn"].startswith("arn:aws:sns:")
    assert await provider.read(Context(), sub) is not None

    await provider.delete(Context(), sub)
    assert await provider.read(Context(), sub) is None


async def test_sns_read_missing_is_none() -> None:
    provider = AwsProvider()
    assert await provider.read(Context(), SnsTopic("t", name="ghost")) is None


async def test_dynamodb_table_crud() -> None:
    provider = AwsProvider()
    res = DynamoDbTable(
        "d", table_name="items", hash_key="pk", range_key="sk", tags={"team": "data"}
    )
    out = await provider.create(Context(), res)
    assert out["arn"].endswith(":table/items")
    schema = boto3.client("dynamodb").describe_table(TableName="items")["Table"]["KeySchema"]
    assert {k["KeyType"] for k in schema} == {"HASH", "RANGE"}

    await provider.update(Context(), out, res)
    await provider.delete(Context(), res)
    assert await provider.read(Context(), res) is None


async def test_cloudwatch_log_group_crud() -> None:
    provider = AwsProvider()
    res = CloudWatchLogGroup("l", log_group_name="/svc/app", retention_days=7)
    out = await provider.create(Context(), res)
    assert out["arn"].startswith("arn:aws:logs:")
    groups = boto3.client("logs").describe_log_groups(logGroupNamePrefix="/svc/app")["logGroups"]
    assert groups[0]["retentionInDays"] == 7

    await provider.update(
        Context(), out, CloudWatchLogGroup("l", log_group_name="/svc/app", retention_days=30)
    )
    groups = boto3.client("logs").describe_log_groups(logGroupNamePrefix="/svc/app")["logGroups"]
    assert groups[0]["retentionInDays"] == 30

    await provider.delete(Context(), res)
    assert await provider.read(Context(), res) is None


async def test_s3_bucket_policy_crud() -> None:
    provider = AwsProvider()
    await provider.create(Context(), S3Bucket("b", bucket="policed"))
    res = S3BucketPolicy(
        "p",
        bucket="policed",
        statements=[allow("s3:GetObject", on="arn:aws:s3:::policed/*", principal="*")],
    )
    assert await provider.create(Context(), res) == {}
    assert await provider.read(Context(), res) is not None

    doc = json.loads(boto3.client("s3").get_bucket_policy(Bucket="policed")["Policy"])
    assert doc["Statement"][0]["Principal"] == "*"

    await provider.delete(Context(), res)
    assert await provider.read(Context(), res) is None


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
    engine = _mixed_engine()
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


# -- CloudFront / ACM / Route53 (new resource types) -------------------------


async def test_origin_access_control_crud() -> None:
    provider = AwsProvider()
    ctx = Context()
    out = await provider.create(
        ctx, OriginAccessControl("o", oac_name="site-oac", description="v1")
    )
    oid = out["oac_id"]
    assert oid
    # id-located: a resource reconstructed from state carries oac_id.
    tracked = OriginAccessControl("o", oac_name="site-oac", oac_id=oid)
    assert await provider.read(ctx, tracked) is not None
    await provider.update(
        ctx,
        {"oac_id": oid},
        OriginAccessControl("o", oac_name="site-oac", description="v2", oac_id=oid),
    )
    got = boto3.client("cloudfront").get_origin_access_control(Id=oid)
    assert got["OriginAccessControl"]["OriginAccessControlConfig"]["Description"] == "v2"
    await provider.delete(ctx, tracked)
    assert await provider.read(ctx, tracked) is None


async def test_cloudfront_distribution_crud() -> None:
    provider = AwsProvider()
    ctx = Context()
    oac = await provider.create(ctx, OriginAccessControl("o", oac_name="d-oac"))
    origin = "b.s3.us-east-1.amazonaws.com"
    out = await provider.create(
        ctx,
        CloudFrontDistribution(
            "cdn", origin_domain=origin, oac_id=oac["oac_id"], comment="v1", tags={"app": "x"}
        ),
    )
    did = out["distribution_id"]
    assert out["domain_name"].endswith(".cloudfront.net")
    assert out["arn"].startswith("arn:aws:cloudfront:")
    tracked = CloudFrontDistribution(
        "cdn", origin_domain=origin, oac_id=oac["oac_id"], distribution_id=did
    )
    assert await provider.read(ctx, tracked) is not None
    await provider.update(
        ctx,
        {"distribution_id": did},
        CloudFrontDistribution(
            "cdn", origin_domain=origin, oac_id=oac["oac_id"], comment="v2", distribution_id=did
        ),
    )
    cfg = boto3.client("cloudfront").get_distribution(Id=did)["Distribution"]["DistributionConfig"]
    assert cfg["Comment"] == "v2"
    # delete drives disable -> poll-until-Deployed -> delete (moto: Deployed at once,
    # and it does not enforce disable-before-delete, so this only asserts it's gone).
    await provider.delete(ctx, tracked)
    assert await provider.read(ctx, tracked) is None


async def test_acm_certificate_crud() -> None:
    provider = AwsProvider()
    ctx = Context()
    out = await provider.create(
        ctx, AcmCertificate("cert", domain_name="ex.example.com", tags={"app": "x"})
    )
    arn = out["arn"]
    assert arn.startswith("arn:aws:acm:us-east-1:")  # handler pins us-east-1
    assert out["validation_type"] == "CNAME"
    assert out["validation_name"] and out["validation_value"]
    tracked = AcmCertificate("cert", domain_name="ex.example.com", arn=arn)
    assert await provider.read(ctx, tracked) is not None
    await provider.delete(ctx, tracked)
    assert await provider.read(ctx, tracked) is None


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


async def test_static_site_graph_through_engine() -> None:
    # bucket + OAC -> distribution -> bucket policy; the policy's OAC condition
    # references the distribution arn, resolved before the policy is written.
    engine = _mixed_engine()
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
