"""Import against AWS: resources nothing atlantide made, adopted and then skipped.

The reconcile-level suite drives a fake provider, so it proves the *row* is right.
This one proves the reads are: it creates a bucket and a VPC with raw boto3 — no
atlantide tags, no state, nothing that would let a lookup cheat — then imports
them and requires the next plan to have nothing to do.

That is the whole promise of the feature, and it is the assertion that would have
failed before ``Ec2Handler.read`` learned to use the id it was given: a VPC with
no managed tag is exactly what the old attribute lookup could not reliably find.
"""

from __future__ import annotations

import boto3

from atlantide.core import Context
from atlantide.providers.aws import AwsProvider
from atlantide.reconcile import Action, ImportRequest, identity_fields
from atlantide.reconcile.adopt import ImportStatus
from tests.support import TEST_REGION, Harness, aws_fixture
from tests.support.factories import globals_of, types_of

aws_env = aws_fixture()

CONFIG = """
S3Bucket('assets', bucket='acme-imported-assets', region='eu-north-1')
Vpc('net', cidr_block='10.42.0.0/16', region='eu-north-1')
"""
BUCKET_ID = "default:aws.S3Bucket:assets"
VPC_ID = "default:aws.Vpc:net"


def _harness() -> Harness:
    from atlantide.providers.aws import S3Bucket, Vpc

    return Harness(
        types=types_of(S3Bucket, Vpc),
        provider=AwsProvider(region=TEST_REGION),
        globals=globals_of(S3Bucket, Vpc),
    )


def _make_unmanaged(*, configured: bool = True) -> str:
    """A bucket and a VPC created outside atlantide entirely.

    ``configured`` gives the bucket the encryption and public-access settings the
    config declares. A bare ``create_bucket`` has neither — S3's defaults are not
    the safe ones this resource type insists on — so leaving it off produces a
    genuine mismatch, which is what :func:`test_a_mismatched_resource_is_refused`
    is about.
    """
    s3 = boto3.client("s3", region_name=TEST_REGION)
    s3.create_bucket(
        Bucket="acme-imported-assets",
        CreateBucketConfiguration={"LocationConstraint": TEST_REGION},
    )
    if configured:
        s3.put_public_access_block(
            Bucket="acme-imported-assets",
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
        s3.put_bucket_encryption(
            Bucket="acme-imported-assets",
            ServerSideEncryptionConfiguration={
                "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
            },
        )
    ec2 = boto3.client("ec2", region_name=TEST_REGION)
    return str(ec2.create_vpc(CidrBlock="10.42.0.0/16")["Vpc"]["VpcId"])


def test_unmanaged_resources_import_and_then_plan_as_noops() -> None:
    """The assertion the feature exists for."""
    vpc_id = _make_unmanaged()
    harness = _harness()

    outcomes = harness.adopt(
        CONFIG,
        BUCKET_ID,  # found by name; needs no id
        ImportRequest(VPC_ID, external_id=vpc_id),  # located by its id
    )
    assert [o.status for o in outcomes] == [ImportStatus.IMPORTED, ImportStatus.IMPORTED]

    changes = harness.diff_only(CONFIG)
    assert {c.action for c in changes.changes} == {Action.NOOP}
    assert harness.backend.load().nodes[VPC_ID].outputs["vpc_id"] == vpc_id


def test_the_import_creates_nothing() -> None:
    """A resource that does not exist must not be conjured into being. Import is
    a read plus a state write, and this is what says so out loud."""
    harness = _harness()
    outcomes = harness.adopt(CONFIG, BUCKET_ID)

    assert [o.status for o in outcomes] == [ImportStatus.NOT_FOUND]
    assert harness.backend.load().nodes == {}
    buckets = boto3.client("s3", region_name=TEST_REGION).list_buckets()["Buckets"]
    assert buckets == []


def test_a_mismatched_resource_is_refused_and_says_what_differs() -> None:
    """A bucket without the encryption config declares is not the bucket config
    describes, and adopting it would make the next apply change it silently."""
    _make_unmanaged(configured=False)
    harness = _harness()

    [outcome] = harness.adopt(CONFIG, BUCKET_ID)
    assert outcome.status is ImportStatus.DRIFTED
    assert outcome.drift is not None
    assert set(outcome.drift.changed) & {"encryption", "block_public_access"}
    assert harness.backend.load().nodes == {}


def test_allow_drift_adopts_the_mismatch_and_the_next_plan_offers_to_fix_it() -> None:
    """The escape hatch, and the reason it is safe: the follow-up is an UPDATE
    that brings the resource up to what config says, not a REPLACE."""
    _make_unmanaged(configured=False)
    harness = _harness()

    [outcome] = harness.adopt(CONFIG, BUCKET_ID, allow_drift=True)
    assert outcome.status is ImportStatus.IMPORTED

    actions = {c.node_id: c.action for c in harness.diff_only(CONFIG).changes}
    assert actions[BUCKET_ID] is Action.UPDATE


def test_an_imported_vpc_is_still_readable_by_the_recorded_id() -> None:
    """The id import recorded has to be the one later operations resolve, or the
    row is adopted and then immediately useless."""
    import asyncio

    from atlantide.providers.aws import Vpc

    vpc_id = _make_unmanaged()
    harness = _harness()
    harness.adopt(CONFIG, ImportRequest(VPC_ID, external_id=vpc_id))

    recorded = harness.backend.load().nodes[VPC_ID].outputs["vpc_id"]
    live = asyncio.run(
        AwsProvider(region=TEST_REGION).read(
            Context(), Vpc("net", cidr_block="10.42.0.0/16", region=TEST_REGION, vpc_id=recorded)
        )
    )
    assert live is not None
    assert live["vpc_id"] == vpc_id


def test_a_type_located_by_an_id_refuses_to_guess_one() -> None:
    """A VPC has no name to be found by, and config cannot know the id AWS chose.
    Importing one without an id would have to fall back to matching on the CIDR —
    which is how you adopt somebody else's VPC."""
    _make_unmanaged()
    harness = _harness()

    [outcome] = harness.adopt(CONFIG, VPC_ID)  # no external id supplied
    assert outcome.status is ImportStatus.BLOCKED
    assert outcome.identity_field == "vpc_id"
    assert "vpc_id" in outcome.detail
    assert harness.backend.load().nodes == {}


def test_the_listing_says_which_types_need_an_id() -> None:
    """What `atlantide import` with no arguments answers, at the layer that
    computes it: a bucket needs nothing, a VPC needs its id."""
    harness = _harness()
    providers = harness._providers()
    _, ir, _graph, _hashes = harness._compile(CONFIG, providers)

    fields = identity_fields(
        ir=ir, types=harness.types, providers=providers, node_ids=[BUCKET_ID, VPC_ID, "nope"]
    )
    assert fields == {BUCKET_ID: None, VPC_ID: "vpc_id", "nope": None}
