"""A poisoned state row must not schedule a REPLACE of an unchanged resource.

``NO_INPUT_HASH`` is the only channel a state-side problem has into the next plan.
Two things write it: ``refresh --write`` when a provider reports drift, and the
executor when a rollback fails part-way. Both mean *this row cannot be trusted,
re-examine the node*, and both are recovery paths run after a failure.

The diff has a second reason for hashes to disagree: an upstream dependency's
resolved value moved while every symbol stayed identical. That change is
attributed to the fields carrying a ``$ref``, since those are the ones whose
value moved.

Poisoning trips the same branch. A ref-bearing field that is also
``immutable()`` (``SecurityGroup.vpc_id``, ``Subnet.vpc_id``,
``Route53Record.zone_id``, ``IamPolicy.role_arn``) turns the manufactured change
into a REPLACE, so recovering from a failed rollback would destroy and recreate a
live security group whose configuration never changed.

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
from atlantide.state import NO_INPUT_HASH, MemoryStateBackend
from tests.support import TEST_REGION, actions_of, aws_fixture

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
    """Write what `refresh --write` and a failed rollback write."""
    for node_id, node in engine.backend.load().nodes.items():
        if needle in node_id:
            engine.backend.put(dc_replace(node, input_hash=NO_INPUT_HASH))


def _actions(engine: Engine, source: str) -> dict[str, Action]:
    plan = engine.plan(source, "c.py").unwrap()
    return {
        node_id.split(":", 1)[1]: action for node_id, action in actions_of(plan.changeset).items()
    }


async def test_an_unpoisoned_graph_is_a_noop(tmp_path: object) -> None:
    """Baseline: an unpoisoned graph plans all NOOPs."""
    engine, source = await _applied(tmp_path)

    assert set(_actions(engine, source).values()) == {Action.NOOP}


async def test_poisoning_does_not_invent_a_change_on_a_ref_field(tmp_path: object) -> None:
    """The node is re-examined, not rewritten.

    Neither config nor any upstream value moved, only trust in the row. A change
    on `content` merely because it carries a `$ref` would report a nonexistent diff.
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
    """A poisoned row must not produce a REPLACE.

    `content` is mutable here, so the worst case is a needless UPDATE. On an
    immutable ref-bearing field such as `SecurityGroup.vpc_id`, the same
    manufactured change becomes a destroy-and-recreate of working infrastructure,
    triggered by the recovery command for a failed rollback.
    """
    engine, source = await _applied(tmp_path)
    _poison(engine, "File:b")

    assert _actions(engine, source)["local.File:b"] is not Action.REPLACE


async def test_a_poisoned_node_still_reaches_the_provider(tmp_path: object) -> None:
    """Not a NOOP either: the row is untrustworthy, so the provider must re-assert
    the desired state. Skipping it would leave the poison in place indefinitely."""
    engine, source = await _applied(tmp_path)
    _poison(engine, "File:b")

    assert _actions(engine, source)["local.File:b"] is Action.UPDATE


async def test_a_real_symbolic_change_still_replaces(tmp_path: object) -> None:
    """An immutable field that genuinely changed is still a REPLACE, poisoned row
    or not."""
    engine, source = await _applied(tmp_path)
    _poison(engine, "File:b")
    moved = source.replace("/b.txt", "/moved.txt")  # `path` is immutable()

    assert _actions(engine, moved)["local.File:b"] is Action.REPLACE


async def test_an_upstream_value_moving_is_still_attributed_to_the_ref(
    tmp_path: object,
) -> None:
    """With an *unpoisoned* row, differing hashes and identical symbols mean an
    upstream resolved value moved, so the ref-bearing field is reported changed.
    """
    engine, source = await _applied(tmp_path)
    changed_upstream = source.replace('content="x"', 'content="y"')

    change = next(
        c
        for c in engine.plan(changed_upstream, "c.py").unwrap().changeset.changes
        if c.node_id.endswith(":b")
    )

    assert "content" in change.changed_fields


# -- poisoned security group --------------------------------------------------


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
    """`SecurityGroup.vpc_id` is immutable *and* in practice always a `$ref`.

    Guards against a poisoned row (e.g. from `atlantide refresh --write`) planning
    a destroy-and-recreate of a live, untouched security group.
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
