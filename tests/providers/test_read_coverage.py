"""How much of each resource a provider's ``read`` actually checks.

``refresh`` can only flag a field the provider reported, so a handler whose read
returns an identifier alone makes "in sync" a claim no API call supported. B4.1
made that visible per node; this makes it *measurable* and stops it regressing.

Two things are asserted, and they check each other:

* a **round-trip** — create with a non-default value, read it back, and require
  the value to return. This is what proves a field is genuinely observed rather
  than merely named somewhere in the handler.
* a **ratchet** — a checked-in table of what each type observes today. It fails
  when coverage falls, which is the only way losing it is ever visible: nothing
  errors, `refresh` just starts saying "in sync" about more things.

**What this does not prove.** Moto's fidelity is the ceiling. A field moto does
not model round-trips trivially here, so a green test shows the handler asks for
the field and maps it back — not that AWS returns it. An opt-in
``ATLANTIDE_E2E_AWS`` suite against a real account is the only thing that would,
and is deliberately not in PR CI.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import boto3
import pytest

from atlantide.core import Context
from atlantide.core.fields import Mutability, field_mutability
from atlantide.core.resource import Resource
from atlantide.providers.aws import (
    AwsProvider,
    CloudWatchLogGroup,
    DynamoDbTable,
    IamPolicy,
    IamRole,
    S3Bucket,
    SecurityGroup,
    SgRule,
    SnsTopic,
    SqsQueue,
    Subnet,
    Vpc,
    allow,
)
from atlantide.providers.aws.handlers import HANDLERS
from tests.support import TEST_REGION, aws_fixture

aws_env = aws_fixture()

_TRUST = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }
    ],
}


#: The types the round-trip below actually creates. Kept separate from
#: :data:`OBSERVED` because parametrisation happens at collection time, before
#: moto is active — building the resources there would be too early.
_SAMPLED = (
    "aws.CloudWatchLogGroup",
    "aws.DynamoDbTable",
    "aws.IamRole",
    "aws.S3Bucket",
    "aws.SnsTopic",
)


#: Input fields each type's ``read`` reports back, as of now.
#:
#: Raise these deliberately. Lowering one means a resource stopped being checked
#: for drift, which is exactly the regression that is otherwise invisible.
#:
#: An empty tuple is an honest record of a type whose read observes nothing but
#: its own existence. Those are listed rather than omitted so the gap has a name
#: — they are what `docs/limitations.md` reports, and what to work on next.
OBSERVED: dict[str, tuple[str, ...]] = {
    "aws.AcmCertificate": (),
    "aws.AwsAvailabilityZones": (),
    "aws.AwsCallerIdentity": (),
    "aws.CloudFrontDistribution": (),
    "aws.CloudWatchLogGroup": ("retention_days", "tags"),
    "aws.DynamoDbTable": ("billing_mode", "tags"),
    "aws.ElasticIp": (),
    "aws.IamPolicy": ("statements",),
    "aws.IamRole": ("assume_role_policy", "description", "tags"),
    "aws.InternetGateway": (),
    "aws.LambdaFunction": ("handler", "memory_size", "role_arn", "runtime", "timeout"),
    "aws.NatGateway": (),
    "aws.OriginAccessControl": (),
    "aws.Route53HostedZone": (),
    "aws.Route53Record": ("alias", "records", "ttl"),
    "aws.RouteTable": (),
    "aws.S3Bucket": ("block_public_access", "encryption", "tags", "versioning"),
    "aws.S3BucketPolicy": (),
    "aws.S3Folder": (),
    "aws.SecurityGroup": ("egress", "ingress"),
    "aws.SnsSubscription": (),
    "aws.SnsTopic": ("tags",),
    "aws.SqsQueue": (
        "dead_letter_target_arn",
        "max_receive_count",
        "message_retention_seconds",
        "receive_wait_time_seconds",
        "tags",
        "visibility_timeout",
    ),
    "aws.Subnet": ("availability_zone", "map_public_ip_on_launch"),
    "aws.Vpc": (),
}


def _inputs(type_name: str) -> set[str]:
    """Every non-computed field of a type — what a read *could* report."""
    cls = HANDLERS[type_name].resource_type
    return {
        name
        for name, mutability in field_mutability(cls).items()
        if mutability is not Mutability.COMPUTED
    }


# -- the ratchet --------------------------------------------------------------


def test_the_table_covers_every_registered_type() -> None:
    """A new handler has to declare its coverage, so one cannot be added with an
    unexamined read that nobody notices for a year."""
    assert set(OBSERVED) == set(HANDLERS), (
        "the coverage table and the handler registry disagree — add the new type "
        "with the fields its read reports (an empty tuple if it reports none)"
    )


@pytest.mark.parametrize("type_name", sorted(OBSERVED))
def test_declared_fields_are_real_fields(type_name: str) -> None:
    """A typo in the table would otherwise claim coverage of a field that does
    not exist, which reads as progress and is not."""
    unknown = set(OBSERVED[type_name]) - _inputs(type_name)
    assert not unknown, f"{type_name} claims to observe non-existent field(s): {unknown}"


@pytest.mark.parametrize("type_name", sorted(name for name, fields in OBSERVED.items() if fields))
def test_a_declared_field_is_named_in_the_handler(type_name: str) -> None:
    """Cheap check covering the types the round-trip below cannot reach.

    A read that stopped returning a field would otherwise leave the table
    asserting coverage that no longer exists, and this file would be the last
    place anyone thought to look.
    """
    source = inspect.getsource(type(HANDLERS[type_name]))
    for field in OBSERVED[type_name]:
        assert f'"{field}"' in source, (
            f"{type_name}'s handler no longer mentions {field!r} — either restore it "
            f"or lower the entry in OBSERVED and accept the lost coverage"
        )


def test_the_types_that_check_nothing_are_named() -> None:
    """A published limitation kept in one place, so it can be pointed at.

    These are the resources for which `refresh` says "in sync" having verified
    only that the thing exists. Shrinking the list is the work; hiding it is not.
    """
    blind = sorted(name for name, fields in OBSERVED.items() if not fields)
    assert len(blind) <= 14, f"more types now check nothing: {blind}"


def test_overall_coverage_does_not_regress() -> None:
    """One number for the whole provider, so a broad erosion shows up even when
    no single type looks much worse."""
    covered = sum(len(fields) for fields in OBSERVED.values())
    total = sum(len(_inputs(name)) for name in OBSERVED)
    fraction = covered / total
    assert fraction >= 0.20, (
        f"drift coverage fell to {fraction:.0%} ({covered}/{total} input fields). "
        f"Raise the floor when it improves; never lower it to make a change pass."
    )


# -- the round-trip -----------------------------------------------------------


def _samples() -> dict[str, tuple[Resource, dict[str, Any]]]:
    """One resource per type with every observed field set to a non-default value.

    Non-default matters: a field that round-trips only because both sides happen
    to hold the AWS default proves nothing about whether the handler read it.

    The second element overrides the expected read value where a handler
    deliberately normalises. It is empty for every type today: a read that cannot
    round-trip its own input is the bug `tests/providers/test_no_phantom_drift.py`
    exists for, so an entry appearing here again is worth arguing about rather
    than accepting.
    """
    return {
        "aws.S3Bucket": (
            S3Bucket(
                "b",
                bucket="atlantide-read-coverage",
                region=TEST_REGION,
                versioning=True,
                encryption="aws:kms",
                block_public_access=False,
                tags={"env": "test"},
            ),
            {},
        ),
        "aws.CloudWatchLogGroup": (
            CloudWatchLogGroup(
                "lg",
                log_group_name="/atlantide/read-coverage",
                region=TEST_REGION,
                retention_days=14,
                tags={"env": "test"},
            ),
            {},
        ),
        "aws.DynamoDbTable": (
            DynamoDbTable(
                "t",
                table_name="atlantide-read-coverage",
                region=TEST_REGION,
                hash_key="pk",
                billing_mode="PAY_PER_REQUEST",
                tags={"env": "test"},
            ),
            {},
        ),
        "aws.IamRole": (
            IamRole(
                "r",
                role_name="atlantide-read-coverage",
                assume_role_policy=json.dumps(_TRUST),
                description="observed",
                tags={"env": "test"},
            ),
            # No override any more. `read` reports the trust policy in the shape
            # config holds it, so this is a plain identity — which is what the
            # override here was quietly hiding, and what let an untouched IAM role
            # report drift forever.
            {},
        ),
        "aws.SnsTopic": (
            SnsTopic("s", name="atlantide-read-coverage", region=TEST_REGION, tags={"env": "test"}),
            {},
        ),
    }


@pytest.mark.parametrize("type_name", sorted(_SAMPLED))
async def test_every_observed_field_survives_a_round_trip(type_name: str) -> None:
    """Create with non-default values, read back, require them to return.

    This is the assertion the ratchet table is shorthand for. A handler that
    claims a field but never asks the API for it fails here, which is the point
    — the table on its own could be aspirational.
    """
    provider = AwsProvider()
    resource, overrides = _samples()[type_name]
    context = Context()

    await provider.create(context, resource)
    live = await provider.read(context, resource)

    assert live is not None, f"{type_name} was created but read reported it missing"
    declared = OBSERVED[type_name]
    assert declared, "a sampled type must declare at least one observed field"
    for field in declared:
        assert field in live, (
            f"{type_name} declares it observes {field!r}, but read did not report it"
        )
        expected = overrides.get(field, getattr(resource, field))
        assert live[field] == expected, (
            f"{type_name}.{field} did not survive the round trip: "
            f"wrote {expected!r}, read {live[field]!r}"
        )


async def test_a_hand_edit_is_actually_detected() -> None:
    """The consequence this whole file exists for, on one concrete resource.

    Nothing above proves a *changed* value is reported — a read that echoed the
    configured value back would pass every round-trip assertion and still say "in
    sync" forever. Editing the resource behind atlantide's back is the only check
    that distinguishes the two.
    """
    provider = AwsProvider()
    role = IamRole(
        "r",
        role_name="atlantide-hand-edited",
        assume_role_policy=json.dumps(_TRUST),
        description="before",
    )
    await provider.create(Context(), role)

    boto3.client("iam", region_name=TEST_REGION).update_role(
        RoleName="atlantide-hand-edited", Description="edited in the console"
    )
    live = await provider.read(Context(), role)

    assert live is not None
    assert live["description"] == "edited in the console"


# -- types needing another resource created first -----------------------------


async def test_an_inline_policy_reports_its_statements() -> None:
    """The permission document itself. A read returning only "the policy exists"
    means a policy widened by hand never shows up as drift."""
    provider = AwsProvider()
    role = IamRole("r", role_name="atlantide-policy-holder", assumed_by="lambda.amazonaws.com")
    role_out = await provider.create(Context(), role)

    policy = IamPolicy(
        "p",
        role_arn=role_out["arn"],
        policy_name="atlantide-read-coverage",
        statements=[allow("s3:GetObject", on="arn:aws:s3:::bucket/*")],
    )
    await provider.create(Context(), policy)
    live = await provider.read(Context(), policy)

    assert live is not None
    assert live["statements"] == [
        {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": "arn:aws:s3:::bucket/*"}
    ]


async def test_a_queue_reports_its_attributes_and_redrive_policy() -> None:
    """Kept out of the table because the redrive policy needs a real DLQ arn —
    and an unset redrive policy is exactly the case that would pass vacuously."""
    provider = AwsProvider()
    dlq = SqsQueue("dlq", queue_name="atlantide-dlq", region=TEST_REGION)
    dlq_out = await provider.create(Context(), dlq)

    queue = SqsQueue(
        "q",
        queue_name="atlantide-read-coverage",
        region=TEST_REGION,
        visibility_timeout=45,
        message_retention_seconds=1209,
        receive_wait_time_seconds=7,
        dead_letter_target_arn=dlq_out["arn"],
        max_receive_count=3,
        tags={"env": "test"},
    )
    await provider.create(Context(), queue)
    live = await provider.read(Context(), queue)

    assert live is not None
    for field in OBSERVED["aws.SqsQueue"]:
        assert live[field] == getattr(queue, field), f"SqsQueue.{field} did not round-trip"


async def test_subnet_reports_its_zone_and_public_ip_setting() -> None:
    """Which zone a subnet is in is not cosmetic — it decides what a workload can
    reach. Needs a VPC first, so it sits outside the table."""
    provider = AwsProvider()
    vpc = Vpc("v", cidr_block="10.8.0.0/16", region=TEST_REGION)
    vpc_out = await provider.create(Context(), vpc)

    subnet = Subnet(
        "s",
        region=TEST_REGION,
        vpc_id=vpc_out["vpc_id"],
        cidr_block="10.8.1.0/24",
        availability_zone=f"{TEST_REGION}a",
        map_public_ip_on_launch=True,
    )
    await provider.create(Context(), subnet)
    live = await provider.read(Context(), subnet)

    assert live is not None
    assert live["availability_zone"] == f"{TEST_REGION}a"
    assert live["map_public_ip_on_launch"] is True


async def test_a_security_group_reports_its_rules() -> None:
    """The rules are the resource. A read that omitted them would mean a group
    opened to the world by hand still reads as in sync."""
    provider = AwsProvider()
    vpc = Vpc("v", cidr_block="10.7.0.0/16", region=TEST_REGION)
    vpc_out = await provider.create(Context(), vpc)

    rule = SgRule(protocol="tcp", from_port=443, to_port=443, cidr_blocks=["10.0.0.0/8"])
    group = SecurityGroup(
        "g",
        group_name="atlantide-read-coverage",
        region=TEST_REGION,
        vpc_id=vpc_out["vpc_id"],
        description="observed",
        ingress=[rule],
    )
    await provider.create(Context(), group)
    live = await provider.read(Context(), group)

    assert live is not None
    assert live["ingress"] == [rule.model_dump()]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
