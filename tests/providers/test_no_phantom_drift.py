"""Applying a config and immediately refreshing must report nothing.

The most basic property `refresh` has, and the one nothing checked. Every drift
test until now took a resource, changed it behind atlantide's back, and asserted
the change was seen. None asserted the opposite: that a resource *nobody touched*
comes back in sync.

That gap hid three separate bugs at once, all with the same shape — a ``read``
returning a value whose *form* differs from the stored input, so exact comparison
never matches:

* ``IamRole.assume_role_policy`` — stored as JSON text (or absent, when
  ``assumed_by`` was used), read back as a parsed document.
* ``SecurityGroup.ingress`` / ``egress`` — stored as ``SgRule`` models, read back
  as plain mappings.
* the default egress rule's description — a friendly string that never reaches
  AWS, because AWS creates that rule itself.

None of them was cosmetic. ``refresh --write`` clears ``input_hash`` for any node
it believes has drifted inputs, so a phantom became a permanent state change, and
the next plan turned it into an UPDATE — or, for a ref-bearing ``immutable()``
field like ``SecurityGroup.vpc_id``, a REPLACE of a live firewall.

This test is deliberately about the *fleet*, not one resource: the bug class
recurs every time a handler learns to report a new field, and a per-handler test
is one someone has to remember to write.
"""

from __future__ import annotations

import pytest

from atlantide.core import ProviderRegistry
from atlantide.engine import Engine
from atlantide.reconcile import Action
from atlantide.reconcile.refresh import Drift
from atlantide.state import MemoryStateBackend
from tests.support import TEST_REGION, aws_fixture

aws_env = aws_fixture()

#: One of everything whose `read` reports input fields, wired the way a real
#: config wires it — refs between resources included, since a ref is what turns
#: a phantom diff into a REPLACE.
CONFIG = f"""
from atlantide.core import Stack
from atlantide.providers.aws import (
    CloudWatchLogGroup, DynamoDbTable, IamPolicy, IamRole, Route53HostedZone,
    Route53Record, S3Bucket, SecurityGroup, SgRule, SnsTopic, SqsQueue, Subnet,
    ServicePrincipal, Vpc, allow,
)
with Stack("s", region={TEST_REGION!r}):
    role = IamRole("r", role_name="phantom-role", assumed_by=ServicePrincipal.Lambda)
    IamPolicy(
        "pol", role_arn=role.arn, policy_name="phantom-pol",
        statements=[allow("s3:GetObject", on="arn:aws:s3:::b/*")],
    )
    S3Bucket("b", bucket="phantom-bucket", versioning=True, tags={{"e": "t"}})
    SqsQueue("q", queue_name="phantom-queue", visibility_timeout=45, tags={{"e": "t"}})
    SnsTopic("t", name="phantom-topic", tags={{"e": "t"}})
    DynamoDbTable("d", table_name="phantom-table", hash_key="pk", tags={{"e": "t"}})
    CloudWatchLogGroup("lg", log_group_name="/phantom/lg", retention_days=14, tags={{"e": "t"}})
    vpc = Vpc("v", cidr_block="10.5.0.0/16")
    Subnet("sn", vpc_id=vpc.vpc_id, cidr_block="10.5.1.0/24",
           availability_zone={TEST_REGION + "a"!r})
    SecurityGroup(
        "sg", group_name="phantom-sg", vpc_id=vpc.vpc_id, description="d",
        ingress=[SgRule(protocol="tcp", from_port=443, to_port=443,
                        cidr_blocks=["10.0.0.0/8"])],
    )
    zone = Route53HostedZone("z", domain="phantom.test")
    Route53Record("rec", zone_id=zone.zone_id, record_name="a.phantom.test",
                  record_type="A", ttl=300, records=["10.0.0.1"])
"""


async def _applied() -> Engine:
    from atlantide.providers.aws import TYPES, AwsProvider

    registry = ProviderRegistry()
    registry.register(AwsProvider(region=TEST_REGION))
    engine = Engine(registry, MemoryStateBackend(), TYPES)
    (await engine.apply(CONFIG, "infra.py")).unwrap()
    return engine


async def test_nothing_drifts_right_after_an_apply() -> None:
    """The property in one line. A failure names every resource that lies."""
    engine = await _applied()

    report = (await engine.refresh()).unwrap()

    lying = {n.node_id.split(":", 1)[1]: sorted(n.changed) for n in report.nodes if n.changed}
    assert not lying, f"resources report drift immediately after being created: {lying}"
    assert all(n.kind is Drift.IN_SYNC for n in report.nodes)


async def test_refreshing_twice_is_still_clean() -> None:
    """A read that mutates the thing it reads, or a first read that primes some
    state the second disagrees with, would show up here and nowhere else."""
    engine = await _applied()

    (await engine.refresh()).unwrap()
    report = (await engine.refresh()).unwrap()

    assert all(n.kind is Drift.IN_SYNC for n in report.nodes)


async def test_refresh_write_does_not_change_what_the_next_plan_does() -> None:
    """The consequence that made these bugs serious rather than noisy.

    `refresh --write` poisons `input_hash` for any node it believes drifted. On a
    phantom that is a permanent state change: the plan afterwards is no longer
    the plan before, and for `SecurityGroup.vpc_id` — immutable, and always a
    `$ref` — the difference was a destroy-and-recreate.
    """
    engine = await _applied()
    before = {c.node_id: c.action for c in engine.plan(CONFIG, "infra.py").unwrap().changeset}
    assert set(before.values()) == {Action.NOOP}, "the baseline plan should be a no-op"

    (await engine.refresh(write=True)).unwrap()

    after = {c.node_id: c.action for c in engine.plan(CONFIG, "infra.py").unwrap().changeset}
    moved = {
        node_id.split(":", 1)[1]: action.value
        for node_id, action in after.items()
        if action is not Action.NOOP
    }
    assert not moved, f"`refresh --write` turned an idle graph into pending work: {moved}"


async def test_a_real_hand_edit_is_still_reported() -> None:
    """The fixes normalize a comparison; they must not blunt it.

    A trust policy rewritten in the console is the change on an IAM role that
    matters most, and the normalization added for the phantom sits directly in
    its path.
    """
    import boto3

    engine = await _applied()
    boto3.client("iam", region_name=TEST_REGION).update_assume_role_policy(
        RoleName="phantom-role",
        PolicyDocument='{"Version": "2012-10-17", "Statement": [{"Effect": "Allow",'
        ' "Principal": {"AWS": "*"}, "Action": "sts:AssumeRole"}]}',
    )

    report = (await engine.refresh()).unwrap()

    role = next(n for n in report.nodes if n.node_id.endswith("IamRole:r"))
    assert role.kind is Drift.DRIFTED
    assert "assume_role_policy" in role.changed


async def test_a_hand_edited_security_group_rule_is_still_reported() -> None:
    """The same check for the other normalization: canonicalizing both sides must
    not make a rule opened to the world compare equal to the declared one."""
    import boto3

    engine = await _applied()
    ec2 = boto3.client("ec2", region_name=TEST_REGION)
    group_id = ec2.describe_security_groups(
        Filters=[{"Name": "group-name", "Values": ["phantom-sg"]}]
    )["SecurityGroups"][0]["GroupId"]
    ec2.authorize_security_group_ingress(
        GroupId=group_id,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
            }
        ],
    )

    report = (await engine.refresh()).unwrap()

    group = next(n for n in report.nodes if n.node_id.endswith("SecurityGroup:sg"))
    assert group.kind is Drift.DRIFTED
    assert "ingress" in group.changed


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
