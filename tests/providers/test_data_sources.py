"""Data sources: reading facts about the account instead of hardcoding them.

Without these, an account id or an availability zone has to be written into the
config, which means the config works in exactly one account — the thing configs
exist to avoid.

The design claim being tested is that a data source needed no new provider
method: it is a resource whose create and update are reads and whose delete is
nothing. What follows from that is what is asserted here — chiefly that
destroying one does not destroy anything real.
"""

from __future__ import annotations

from pathlib import Path

import boto3
import pytest

from atlantide.core import Context
from atlantide.ir import lower
from atlantide.lang import evaluate_source
from atlantide.providers.aws import AwsAvailabilityZones, AwsCallerIdentity, AwsProvider
from atlantide.reconcile import Action
from atlantide.state import SqliteStateBackend
from tests.support import TEST_REGION, Cli, aws_fixture

cli = Cli()


# Autouse: moto + AWS creds + a "default" stack supplying the region, so a
# lookup declared without one still resolves — the same kit `test_aws` uses.
aws_env = aws_fixture()


GLOBALS = {
    "AwsCallerIdentity": AwsCallerIdentity,
    "AwsAvailabilityZones": AwsAvailabilityZones,
}


# -- the lookups themselves ---------------------------------------------------


async def test_caller_identity_reports_the_account() -> None:
    """The missing piece in every hand-built ARN."""
    provider = AwsProvider()
    identity = AwsCallerIdentity("me", region=TEST_REGION)

    out = await provider.create(Context(), identity)

    assert out["account_id"].isdigit()
    assert out["arn"].startswith("arn:aws:")


async def test_availability_zones_are_listed_in_a_stable_order() -> None:
    """The API promises no order, and a config that indexes into the list would
    otherwise put a subnet in a different zone on a whim."""
    provider = AwsProvider()
    zones = AwsAvailabilityZones("azs", region=TEST_REGION)

    first = await provider.create(Context(), zones)
    second = await provider.read(Context(), zones)

    assert first["names"] == sorted(first["names"])
    assert second is not None and second["names"] == first["names"]


async def test_a_lookup_reads_the_same_way_on_create_and_read() -> None:
    """Create, update and read are one operation for something that already
    exists — which is why no fifth provider method was needed."""
    provider = AwsProvider()
    identity = AwsCallerIdentity("me", region=TEST_REGION)

    created = await provider.create(Context(), identity)
    read = await provider.read(Context(), identity)
    updated = await provider.update(Context(), created, identity)

    assert created == read == updated


async def test_deleting_a_lookup_destroys_nothing() -> None:
    """The one thing a data source must never do. `delete` is a no-op, so this
    simply must not raise and must leave the account exactly as it was."""
    provider = AwsProvider()
    identity = AwsCallerIdentity("me", region=TEST_REGION)
    await provider.create(Context(), identity)

    await provider.delete(Context(), identity)

    assert await provider.read(Context(), identity) is not None


# -- how the graph treats them ------------------------------------------------


def _ir(source: str) -> object:
    return lower(evaluate_source(source, extra_globals=GLOBALS).unwrap())


def test_a_data_node_is_marked_as_one_in_the_ir() -> None:
    ir = _ir("AwsCallerIdentity('me', region='eu-north-1')\n")
    assert ir.nodes[0].kind == "data"


def test_a_managed_resource_is_not() -> None:
    from atlantide.providers.aws import S3Bucket

    ir = lower(
        evaluate_source(
            "S3Bucket('b', bucket='a-managed-bucket', region='eu-north-1')\n",
            extra_globals={"S3Bucket": S3Bucket},
        ).unwrap()
    )
    assert ir.nodes[0].kind == "resource"


def test_the_kind_is_part_of_identity() -> None:
    """Unlike `aliases` and `depends_on`, this belongs in the hash: the same
    query as a lookup and as a managed resource are different things, and turning
    one into the other must not read as an in-place update."""
    from atlantide.ir.model import IRNode

    query = {
        "id": "n",
        "type": "t",
        "provider": "p",
        "provider_version": "1",
        "properties": {},
        "dependencies": (),
    }
    assert IRNode(**query, kind="data").to_canonical() != IRNode(**query).to_canonical()


# -- end to end ---------------------------------------------------------------


def test_a_config_can_build_an_arn_from_the_account_id(tmp_path: Path) -> None:
    """The motivating case: portable across accounts."""
    cfg = tmp_path / "config.py"
    cfg.write_text(
        "from atlantide.core import output\n"
        "from atlantide.providers.aws import AwsCallerIdentity\n"
        f"me = AwsCallerIdentity('me', region={TEST_REGION!r})\n"
        "output('account', me.account_id)\n"
    )
    state = tmp_path / "s.db"

    cli.ok("apply", cfg, "--state", state, "--region", TEST_REGION, "-y")

    shown = cli.ok("output", "account", "--state", state)
    assert shown.stdout.strip().isdigit()


def test_re_applying_a_lookup_is_a_noop(tmp_path: Path) -> None:
    """Read once and pinned in state: a plan performs no provider call, so an
    unchanged lookup does not churn."""
    cfg = tmp_path / "config.py"
    cfg.write_text(
        "from atlantide.providers.aws import AwsCallerIdentity\n"
        f"AwsCallerIdentity('me', region={TEST_REGION!r})\n"
    )
    state = tmp_path / "s.db"
    cli.ok("apply", cfg, "--state", state, "--region", TEST_REGION, "-y")

    again = cli.run("plan", cfg, "--state", state)
    assert "1 unchanged" in again.output


def test_destroying_a_config_forgets_its_lookups(tmp_path: Path) -> None:
    """The row goes; nothing is called. A destroy that reached the provider would
    be asking AWS to delete an availability zone."""
    cfg = tmp_path / "config.py"
    cfg.write_text(
        "from atlantide.providers.aws import AwsAvailabilityZones\n"
        f"AwsAvailabilityZones('azs', region={TEST_REGION!r})\n"
    )
    state = tmp_path / "s.db"
    cli.ok("apply", cfg, "--state", state, "--region", TEST_REGION, "-y")

    cli.run("destroy", "--state", state, "--region", TEST_REGION, "-y")

    backend = SqliteStateBackend(str(state))
    try:
        assert backend.load().nodes == {}
    finally:
        backend.close()
    # The zones are, unsurprisingly, still there.
    assert boto3.client("ec2", region_name=TEST_REGION).describe_availability_zones()[
        "AvailabilityZones"
    ]


def test_a_changed_query_re_reads_rather_than_replacing(tmp_path: Path) -> None:
    """There is nothing to replace: no resource was created, and
    destroy-then-create would call `delete` on something atlantide does not own.
    """
    cfg = tmp_path / "config.py"
    cfg.write_text(
        "from atlantide.providers.aws import AwsAvailabilityZones\n"
        f"AwsAvailabilityZones('azs', region={TEST_REGION!r}, state='available')\n"
    )
    state = tmp_path / "s.db"
    cli.ok("apply", cfg, "--state", state, "--region", TEST_REGION, "-y")

    cfg.write_text(
        "from atlantide.providers.aws import AwsAvailabilityZones\n"
        f"AwsAvailabilityZones('azs', region={TEST_REGION!r}, state='impaired')\n"
    )
    result = cli.ok("plan", cfg, "--state", state)
    assert "update" in result.output
    assert "replace" not in result.output


def test_a_lookup_appears_in_the_type_registry() -> None:
    """`TYPES` used to be derived strictly from the CRUD registry — "a type
    cannot exist without CRUD" — which structurally forbade a managed-free type.
    The rule is now "no type without a handler"."""
    from atlantide.providers.aws import TYPES

    assert "aws.AwsCallerIdentity" in TYPES
    assert "aws.AwsAvailabilityZones" in TYPES


def test_the_action_for_a_new_lookup_is_a_create(tmp_path: Path) -> None:
    """It reads as an ordinary create because that is what the first read is —
    nothing about the executor needed a special case."""
    cfg = tmp_path / "config.py"
    cfg.write_text(
        "from atlantide.providers.aws import AwsCallerIdentity\n"
        f"AwsCallerIdentity('me', region={TEST_REGION!r})\n"
    )
    result = cli.run("plan", cfg, "--state", tmp_path / "s.db")
    assert Action.CREATE.value in result.output


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
