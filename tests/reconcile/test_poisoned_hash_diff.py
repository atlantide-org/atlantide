"""A poisoned state row must not schedule a REPLACE of a resource that is fine.

``NO_INPUT_HASH`` is the one channel a state-side problem has into the next plan.
Two things write it: ``refresh --write`` when a provider reports drift, and the
executor when a rollback fails part-way. Both mean the same thing — *this row can
no longer be trusted, look at the node again* — and both are recovery paths a
user is told to run after something has already gone wrong.

The diff has a second reason for hashes to disagree: an upstream dependency's
resolved value moved while every symbol stayed identical. That case is real, and
it is attributed to the fields carrying a ``$ref`` because those are the ones
whose value moved.

Poisoning trips that same branch, and the consequence is not cosmetic. A
ref-bearing field that is also ``immutable()`` — ``SecurityGroup.vpc_id``,
``Subnet.vpc_id``, ``Route53Record.zone_id``, ``IamPolicy.role_arn`` — turns the
manufactured change into a REPLACE. The recovery step for a failed rollback would
then destroy and recreate a live security group whose configuration never changed.

The `conditional` flag on such a REPLACE is presentational only: it renders as
"known after apply" and the executor replaces regardless.
"""

from __future__ import annotations

from dataclasses import replace as dc_replace

import pytest

from atlantide.core import ProviderRegistry
from atlantide.engine import Engine
from atlantide.providers.local import TYPES, LocalProvider
from atlantide.reconcile import Action
from atlantide.state import MemoryStateBackend
from atlantide.state.backend import NO_INPUT_HASH
from tests.support import TEST_REGION, aws_fixture

#: `b.content` reads `a.checksum`, a computed output, so it lowers to a `$ref`.
CONFIG = """
from atlantide.core import Stack
from atlantide.providers.local import File
with Stack("s", region="eu-north-1"):
    a = File("a", path="{tmp}/a.txt", content="x")
    File("b", path="{tmp}/b.txt", content=a.checksum)
"""


async def _applied(tmp_path: object) -> tuple[Engine, str]:
    registry = ProviderRegistry()
    registry.register(LocalProvider())
    engine = Engine(registry, MemoryStateBackend(), TYPES)
    source = CONFIG.format(tmp=tmp_path)
    (await engine.apply(source, "c.py")).unwrap()
    return engine, source


def _poison(engine: Engine, needle: str) -> None:
    """Exactly what `refresh --write` and a failed rollback write."""
    for node_id, node in engine.backend.load().nodes.items():
        if needle in node_id:
            engine.backend.put(dc_replace(node, input_hash=NO_INPUT_HASH))


def _actions(engine: Engine, source: str) -> dict[str, Action]:
    plan = engine.plan(source, "c.py").unwrap()
    return {change.node_id.split(":", 1)[1]: change.action for change in plan.changeset.changes}


async def test_an_unpoisoned_graph_is_a_noop(tmp_path: object) -> None:
    """The baseline the rest of the file is measured against."""
    engine, source = await _applied(tmp_path)

    assert set(_actions(engine, source).values()) == {Action.NOOP}


async def test_poisoning_does_not_invent_a_change_on_a_ref_field(tmp_path: object) -> None:
    """The node is re-examined, not rewritten.

    Nothing about the config moved and no upstream value moved either — only the
    trust in the row did. Manufacturing a change on `content` because it happens
    to carry a `$ref` reports a diff that does not exist.
    """
    engine, source = await _applied(tmp_path)
    _poison(engine, "File:b")

    change = next(
        c
        for c in engine.plan(source, "c.py").unwrap().changeset.changes
        if c.node_id.endswith(":b")
    )

    assert change.changed_fields == (), (
        f"poisoning invented a change on {list(change.changed_fields)} — the symbols "
        f"are identical on both sides"
    )


async def test_a_poisoned_node_is_never_replaced(tmp_path: object) -> None:
    """The consequence, and the reason this matters more than a cosmetic diff.

    `content` is mutable here, so the worst case is a needless UPDATE. Make the
    ref-bearing field immutable — as `SecurityGroup.vpc_id` is — and the same
    manufactured change becomes a destroy-and-recreate of working infrastructure,
    triggered by the command the docs recommend running after a failed rollback.
    """
    engine, source = await _applied(tmp_path)
    _poison(engine, "File:b")

    assert _actions(engine, source)["local.File:b"] is not Action.REPLACE


async def test_a_poisoned_node_still_reaches_the_provider(tmp_path: object) -> None:
    """Not a NOOP either. The row was declared untrustworthy, so the point is to
    let the provider re-assert the desired state — skipping it would leave the
    poison in place forever and defeat the channel entirely."""
    engine, source = await _applied(tmp_path)
    _poison(engine, "File:b")

    assert _actions(engine, source)["local.File:b"] is Action.UPDATE


async def test_a_real_symbolic_change_still_replaces(tmp_path: object) -> None:
    """The fix must not blunt the ordinary path: an immutable field that genuinely
    changed is still a REPLACE, poisoned row or not."""
    engine, source = await _applied(tmp_path)
    _poison(engine, "File:b")
    moved = source.replace("/b.txt", "/moved.txt")  # `path` is immutable()

    assert _actions(engine, moved)["local.File:b"] is Action.REPLACE


async def test_an_upstream_value_moving_is_still_attributed_to_the_ref(
    tmp_path: object,
) -> None:
    """The branch this shares still has its real job.

    With an *unpoisoned* row, differing hashes and identical symbols do mean an
    upstream resolved value moved, and blaming the ref-bearing field is right.
    """
    engine, source = await _applied(tmp_path)
    changed_upstream = source.replace('content="x"', 'content="y"')

    change = next(
        c
        for c in engine.plan(changed_upstream, "c.py").unwrap().changeset.changes
        if c.node_id.endswith(":b")
    )

    assert "content" in change.changed_fields


# -- the case that made this urgent -------------------------------------------


aws_env = aws_fixture()

AWS_CONFIG = f"""
from atlantide.core import Stack
from atlantide.providers.aws import Vpc, SecurityGroup, SgRule
with Stack("s", region={TEST_REGION!r}):
    vpc = Vpc("v", cidr_block="10.5.0.0/16")
    SecurityGroup(
        "sg", group_name="poison-sg", vpc_id=vpc.vpc_id, description="d",
        ingress=[SgRule(protocol="tcp", from_port=443, to_port=443,
                        cidr_blocks=["10.0.0.0/8"])],
    )
"""


async def test_a_poisoned_security_group_is_not_destroyed_and_recreated() -> None:
    """`SecurityGroup.vpc_id` is immutable *and* always a `$ref` in practice.

    That combination is what turned a poisoned row into a plan to delete a live
    firewall and build a new one — from `atlantide refresh --write`, on
    infrastructure nobody had touched.
    """
    from atlantide.providers.aws import TYPES as AWS_TYPES
    from atlantide.providers.aws import AwsProvider

    registry = ProviderRegistry()
    registry.register(AwsProvider(region=TEST_REGION))
    engine = Engine(registry, MemoryStateBackend(), AWS_TYPES)
    (await engine.apply(AWS_CONFIG, "c.py")).unwrap()

    _poison(engine, "SecurityGroup")

    actions = _actions(engine, AWS_CONFIG)
    assert actions["aws.SecurityGroup:sg"] is not Action.REPLACE, (
        "a poisoned security group planned a destroy-and-recreate of a live firewall"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
