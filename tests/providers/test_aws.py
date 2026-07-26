"""AWS S3Bucket provider under moto + a mixed local+aws graph through the engine."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError

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
from atlantide.providers.aws.handlers.observability import CloudWatchLogGroupHandler
from atlantide.providers.aws.resources.compute import package_bytes
from atlantide.providers.local import LocalProvider
from tests.conftest import make_engine
from tests.support import aws_fixture

_TRUST_POLICY = (
    '{"Version": "2012-10-17", "Statement": [{"Effect": "Allow",'
    ' "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]}'
)

# Autouse: moto + AWS creds + a "default" stack (resources require a region; the
# stack supplies it and keeps the node-id prefix "default"). The cloud-test kit
# makes this reusable — a second provider swaps env + mock_factory.
aws_env = aws_fixture(region="us-east-1")


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


async def test_sqs_create_with_changed_attributes_adopts_the_existing_queue() -> None:
    """A re-run create (state row never persisted) whose attributes differ from
    the live queue's answers QueueAlreadyExists; it adopts by name rather than
    failing the apply."""
    provider = AwsProvider()
    out = await provider.create(Context(), SqsQueue("q", queue_name="jobs", visibility_timeout=30))
    again = await provider.create(
        Context(), SqsQueue("q", queue_name="jobs", visibility_timeout=60)
    )
    assert again == out


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


@pytest.fixture
def package(tmp_path: Path) -> str:
    """A real deployment package on disk. Lambda has no placeholder any more:
    a function with no code is refused rather than silently shipped empty."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "index.py").write_text("def handler(event, context):\n    return {}\n")
    return str(source)


async def test_lambda_create_read_update_delete(package: str) -> None:
    provider = AwsProvider()
    role_out = await provider.create(
        Context(), IamRole("r", role_name="fn-role", assume_role_policy=_LAMBDA_TRUST)
    )
    res = LambdaFunction(
        "f",
        function_name="fn",
        role_arn=role_out["arn"],
        tags={"env": "t"},
        code_path=package,
    )
    out = await provider.create(Context(), res)
    assert out["arn"].endswith(":function:fn")
    assert await provider.read(Context(), res) is not None

    await provider.update(
        Context(),
        out,
        LambdaFunction(
            "f",
            function_name="fn",
            role_arn=role_out["arn"],
            runtime="python3.11",
            code_path=package,
        ),
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


async def test_sns_subscription_survives_its_topic_deleted_out_of_band() -> None:
    """Listing subscriptions of a deleted topic raises NotFound; that is absence
    (the subscriptions died with the topic), so read reports None and delete is
    a no-op — not a raised apply."""
    provider = AwsProvider()
    topic_out = await provider.create(Context(), SnsTopic("t", name="doomed"))
    queue_out = await provider.create(Context(), SqsQueue("q", queue_name="doomed-q"))
    sub = SnsSubscription("s", topic_arn=topic_out["arn"], endpoint=queue_out["arn"])
    await provider.create(Context(), sub)

    boto3.client("sns").delete_topic(TopicArn=topic_out["arn"])

    assert await provider.read(Context(), sub) is None
    await provider.delete(Context(), sub)  # idempotent, like every other handler


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


# -- create idempotency -----------------------------------------------------
#
# A create is re-run whenever its state row never reached `created`: the process
# was killed between the AWS call and the persist, or a sibling node failed and
# cancelled the task. By then the resource may exist with nothing recording it,
# so an unconditional create either duplicates it or fails on a name clash.


async def test_ec2_create_adopts_its_own_interrupted_create() -> None:
    provider = AwsProvider()
    vpc = Vpc("v", cidr_block="10.0.0.0/16")
    first = (await provider.create(Context(), vpc))["vpc_id"]

    again = (await provider.create(Context(), vpc))["vpc_id"]
    assert again == first, "the retry must adopt, not make a second VPC"


async def test_ec2_create_does_not_adopt_a_stranger_sharing_a_cidr() -> None:
    """EC2 attributes are not unique, so adoption is keyed on the node tag."""
    provider = AwsProvider()
    boto3.client("ec2").create_vpc(CidrBlock="10.0.0.0/16")  # someone else's

    ours = (await provider.create(Context(), Vpc("v", cidr_block="10.0.0.0/16")))["vpc_id"]
    vpcs = boto3.client("ec2").describe_vpcs()["Vpcs"]
    assert sum(1 for v in vpcs if v["CidrBlock"] == "10.0.0.0/16") == 2
    assert ours in {v["VpcId"] for v in vpcs}


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


async def test_lambda_create_adopts_instead_of_erroring(package: str) -> None:
    provider = AwsProvider()
    role = await provider.create(
        Context(), IamRole("r", role_name="adopt-role", assume_role_policy=_LAMBDA_TRUST)
    )
    fn = LambdaFunction("f", function_name="adopt-fn", role_arn=role["arn"], code_path=package)
    first = await provider.create(Context(), fn)
    assert await provider.create(Context(), fn) == first


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


async def test_cloudfront_distribution_rerun_create_adopts_by_caller_reference() -> None:
    """The stable CallerReference makes a re-run create answer
    DistributionAlreadyExists; the handler adopts the distribution holding the
    reference instead of surfacing the conflict."""
    provider = AwsProvider()
    ctx = Context()
    oac = await provider.create(ctx, OriginAccessControl("o", oac_name="dup-oac"))
    origin = "b.s3.us-east-1.amazonaws.com"
    first = await provider.create(
        ctx, CloudFrontDistribution("cdn", origin_domain=origin, oac_id=oac["oac_id"])
    )

    again = await provider.create(
        ctx, CloudFrontDistribution("cdn", origin_domain=origin, oac_id=oac["oac_id"])
    )

    assert again["distribution_id"] == first["distribution_id"]
    listing = boto3.client("cloudfront").list_distributions()["DistributionList"]
    assert len(listing.get("Items", [])) == 1, "adopted, not duplicated"


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


def test_a_log_group_is_found_past_the_first_page() -> None:
    """`describe_log_groups` filters by prefix and pages at 50.

    A single unpaginated request only finds the group when fewer than 50 others
    share its prefix — which no config controls and nothing warns about. Missing
    it does not degrade gracefully: `read` returns None, refresh calls the node
    MISSING, and `refresh --write` deletes the state row for a log group that is
    sitting there perfectly healthy.

    Driven against a stub rather than moto, which does not enforce the page
    limit — so a moto-only test passes whether or not the handler pages at all,
    which is precisely the bug.
    """

    class PagingLogs:
        """A `logs` client that pages the way the real API does."""

        def __init__(self) -> None:
            first = [{"logGroupName": f"/svc/shared-{i:03d}", "arn": "a"} for i in range(50)]
            second = [{"logGroupName": "/svc/shared-target", "arn": "arn::target"}]
            self.pages = [{"logGroups": first}, {"logGroups": second}]

        def describe_log_groups(self, **_kw: Any) -> dict[str, Any]:
            return self.pages[0]  # one request only ever sees page one

        def get_paginator(self, _name: str) -> Any:
            pages = self.pages

            class Paginator:
                def paginate(self, **_kw: Any) -> Any:
                    return iter(pages)

            return Paginator()

    found = CloudWatchLogGroupHandler._find(PagingLogs(), "/svc/shared-target")

    assert found is not None, "the group exists but was not found past page one"
    assert found["arn"] == "arn::target"


async def test_a_log_group_read_reports_the_inputs_it_can_check() -> None:
    """A read returning only the arn makes refresh say "in sync" about a retention
    policy it never looked at."""
    provider = AwsProvider()
    res = CloudWatchLogGroup(
        "l", log_group_name="/svc/observed", retention_days=14, tags={"env": "prod"}
    )
    await provider.create(Context(), res)

    live = await provider.read(Context(), res)

    assert live is not None
    assert live["retention_days"] == 14
    assert live["tags"] == {"env": "prod"}


async def test_a_log_group_that_is_really_gone_still_reads_as_missing() -> None:
    """The pagination fix must not turn every read into a false positive."""
    provider = AwsProvider()
    res = CloudWatchLogGroup("l", log_group_name="/svc/never-made", retention_days=7)
    assert await provider.read(Context(), res) is None


async def test_a_denied_dynamodb_update_is_reported_not_swallowed() -> None:
    """`update_table` used to be wrapped in a blanket `suppress(ClientError)`.

    An AccessDenied or a throttle then read as success: the apply reported the
    table updated, state recorded the new billing mode, and the table kept the
    old one — with nothing anywhere saying so. Only the genuine "nothing to
    change" response is tolerable here.
    """
    provider = AwsProvider()
    res = DynamoDbTable("t", table_name="denied", hash_key="id")
    out = await provider.create(Context(), res)

    # The client the dispatcher will actually hand the handler, not a
    # look-alike from a different (alias, service, region) cache entry.
    _handler, client = provider._dispatch(res, "update")

    def denied(**_kw: Any) -> None:
        raise ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}},
            "UpdateTable",
        )

    client.update_table = denied
    with pytest.raises(ProviderError):
        await provider.update(Context(), out, res)


async def test_an_unchanged_dynamodb_billing_mode_is_still_a_no_op() -> None:
    """The one response that must stay tolerated: an update touching only tags
    legitimately reports that there is nothing to change."""
    provider = AwsProvider()
    res = DynamoDbTable("t", table_name="unchanged", hash_key="id", tags={"a": "1"})
    out = await provider.create(Context(), res)

    updated = await provider.update(
        Context(), out, DynamoDbTable("t", table_name="unchanged", hash_key="id", tags={"a": "2"})
    )
    assert updated["arn"] == out["arn"]


# -- lambda code source -------------------------------------------------------


async def test_a_lambda_with_no_code_is_refused(package: str) -> None:
    """The trap this replaces: the handler used to ship a hardcoded placeholder
    zip for *every* function.

    That deploys, reports success, and then fails at the first invocation running
    code nobody wrote — the worst shape a failure can take, because every signal
    up to that point says it worked.
    """
    provider = AwsProvider()
    role = await provider.create(
        Context(), IamRole("r", role_name="nocode-role", assume_role_policy=_LAMBDA_TRUST)
    )
    fn = LambdaFunction("f", function_name="nocode", role_arn=role["arn"])

    with pytest.raises(ProviderError, match="has no code"):
        await provider.create(Context(), fn)


async def test_the_deployed_bytes_are_the_ones_on_disk(package: str) -> None:
    """The point of the whole change."""
    provider = AwsProvider()
    role = await provider.create(
        Context(), IamRole("r", role_name="real-role", assume_role_policy=_LAMBDA_TRUST)
    )
    (Path(package) / "index.py").write_text("def handler(e, c):\n    return 'mine'\n")
    fn = LambdaFunction("f", function_name="real", role_arn=role["arn"], code_path=package)
    await provider.create(Context(), fn)

    import zipfile as _zip

    shipped = boto3.client("lambda").get_function(FunctionName="real")
    assert shipped["Configuration"]["FunctionName"] == "real"
    # The package the resource fingerprinted is a zip of what is on disk.
    archive = _zip.ZipFile(io.BytesIO(package_bytes(Path(package))))
    assert archive.read("index.py").decode() == "def handler(e, c):\n    return 'mine'\n"


def test_the_fingerprint_changes_with_the_code(tmp_path: Path) -> None:
    """`code_sha256` is the input the diff watches, so it has to move when a byte
    does — otherwise a code change plans as NOOP and never ships."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "index.py").write_text("one")
    first = LambdaFunction(
        "f", function_name="fp", role_arn="arn:x", region="eu-north-1", code_path=str(source)
    ).code_sha256

    (source / "index.py").write_text("two")
    second = LambdaFunction(
        "f", function_name="fp", role_arn="arn:x", region="eu-north-1", code_path=str(source)
    ).code_sha256

    assert first != second


def test_the_fingerprint_is_stable_for_identical_trees(tmp_path: Path) -> None:
    """Two checkouts of the same code must fingerprint alike, or every plan on a
    fresh clone shows a spurious update. File mtimes differ between checkouts,
    which is why the zip pins its timestamps."""
    contents = {"index.py": "def handler(e, c): pass\n", "lib/util.py": "X = 1\n"}
    digests = []
    for nth in ("a", "b"):
        root = tmp_path / nth
        for name, text in contents.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        digests.append(
            LambdaFunction(
                "f",
                function_name="fp",
                role_arn="arn:x",
                region="eu-north-1",
                code_path=str(root),
            ).code_sha256
        )
    assert digests[0] == digests[1]


def test_a_missing_code_path_is_caught_at_config_time(tmp_path: Path) -> None:
    """Before any provider call, so the error names the config rather than
    arriving half-way through an apply."""
    with pytest.raises(LanguageError, match="does not exist"):
        LambdaFunction(
            "f",
            function_name="fp",
            role_arn="arn:x",
            region="eu-north-1",
            code_path=str(tmp_path / "nope"),
        )


def test_code_path_and_s3_bucket_are_mutually_exclusive(tmp_path: Path) -> None:
    with pytest.raises(LanguageError, match="not both"):
        LambdaFunction(
            "f",
            function_name="fp",
            role_arn="arn:x",
            region="eu-north-1",
            code_path=str(tmp_path),
            s3_bucket="b",
            s3_key="k",
        )


def test_an_s3_source_needs_a_key() -> None:
    with pytest.raises(LanguageError, match="s3_key"):
        LambdaFunction(
            "f", function_name="fp", role_arn="arn:x", region="eu-north-1", s3_bucket="b"
        )


async def test_a_lambda_read_reports_the_config_it_can_check(package: str) -> None:
    provider = AwsProvider()
    role = await provider.create(
        Context(), IamRole("r", role_name="obs-role", assume_role_policy=_LAMBDA_TRUST)
    )
    fn = LambdaFunction(
        "f",
        function_name="observed",
        role_arn=role["arn"],
        code_path=package,
        memory_size=512,
        timeout=42,
    )
    await provider.create(Context(), fn)

    live = await provider.read(Context(), fn)

    assert live is not None
    assert live["memory_size"] == 512
    assert live["timeout"] == 42
    assert live["handler"] == "index.handler"


# -- s3 bucket safety ---------------------------------------------------------


async def test_destroying_a_bucket_with_objects_fails_without_force_destroy() -> None:
    """S3 refuses to delete a non-empty bucket. Without `force_destroy` there is
    no way through it from config, so the whole stack wedges on teardown."""
    provider = AwsProvider()
    res = S3Bucket("b", bucket="has-stuff")
    await provider.create(Context(), res)
    boto3.client("s3").put_object(Bucket="has-stuff", Key="a.txt", Body=b"x")

    with pytest.raises(ProviderError, match="not empty"):
        await provider.delete(Context(), res)


async def test_force_destroy_empties_the_bucket_first() -> None:
    provider = AwsProvider()
    res = S3Bucket("b", bucket="disposable", force_destroy=True)
    await provider.create(Context(), res)
    client = boto3.client("s3")
    for index in range(5):
        client.put_object(Bucket="disposable", Key=f"k{index}.txt", Body=b"x")

    await provider.delete(Context(), res)

    assert await provider.read(Context(), res) is None


async def test_force_destroy_clears_versions_and_delete_markers() -> None:
    """On a versioned bucket, deleting objects only adds delete markers — the
    bucket is still not empty and `delete_bucket` still refuses."""
    provider = AwsProvider()
    res = S3Bucket("b", bucket="versioned-disposable", versioning=True, force_destroy=True)
    await provider.create(Context(), res)
    client = boto3.client("s3")
    client.put_object(Bucket="versioned-disposable", Key="k.txt", Body=b"one")
    client.put_object(Bucket="versioned-disposable", Key="k.txt", Body=b"two")
    client.delete_object(Bucket="versioned-disposable", Key="k.txt")  # a delete marker

    await provider.delete(Context(), res)

    assert await provider.read(Context(), res) is None


async def test_a_bucket_is_private_and_encrypted_unless_told_otherwise() -> None:
    """The defaults are the safe ones: a public or unencrypted bucket should be
    something someone wrote down, not something they forgot."""
    provider = AwsProvider()
    res = S3Bucket("b", bucket="safe-by-default")
    await provider.create(Context(), res)

    live = await provider.read(Context(), res)

    assert live is not None
    assert live["block_public_access"] is True
    assert live["encryption"] == "AES256"


async def test_the_safe_defaults_can_be_turned_off_explicitly() -> None:
    provider = AwsProvider()
    res = S3Bucket("b", bucket="deliberately-open", block_public_access=False, encryption=None)
    await provider.create(Context(), res)

    live = await provider.read(Context(), res)

    assert live is not None
    assert live["block_public_access"] is False
    assert live["encryption"] is None


async def test_bucket_drift_on_the_safety_settings_is_observable() -> None:
    """A bucket opened up in the console has to show as drift, which means the
    read must report these fields rather than only the arn."""
    provider = AwsProvider()
    res = S3Bucket("b", bucket="opened-later")
    await provider.create(Context(), res)
    boto3.client("s3").delete_public_access_block(Bucket="opened-later")

    live = await provider.read(Context(), res)

    assert live is not None
    assert live["block_public_access"] is False, "the change is visible to refresh"
