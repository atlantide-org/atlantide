"""Lifecycle: ignore_changes suppresses diffs; create_before_destroy reorders REPLACE."""

from __future__ import annotations

from atlantide.engine import Engine
from atlantide.ir import lower
from atlantide.ir.model import IRNode
from atlantide.lang import evaluate_source
from atlantide.reconcile import Action, Change, ChangeSet
from atlantide.state import MemoryStateBackend, StateNode
from tests.support import Server, engine_for

from .conftest import GLOBALS, Harness

A = "default:test.Box:a"


# -- ignore_changes ----------------------------------------------------------


def test_ignore_changes_makes_a_changed_field_a_noop(tmp_path: object) -> None:
    lc = "lifecycle=Lifecycle(ignore_changes=['label'])"
    h = Harness(MemoryStateBackend())
    h.apply(f"Box('a', size=1, label='x', {lc})\n")
    h.fake().reset()
    report = h.apply(f"Box('a', size=1, label='y', {lc})\n")
    assert report.noop == [A]  # label drift ignored -> Merkle NOOP
    assert h.fake().calls == []  # no provider touch


def test_without_ignore_changes_the_same_edit_updates(tmp_path: object) -> None:
    h = Harness(MemoryStateBackend())
    h.apply("Box('a', size=1, label='x')\n")
    h.fake().reset()
    report = h.apply("Box('a', size=1, label='y')\n")
    assert report.updated == [A]  # control: normally an UPDATE


# -- create_before_destroy ---------------------------------------------------


def test_cbd_replace_creates_before_destroying(tmp_path: object) -> None:
    h = Harness(MemoryStateBackend())
    h.apply("Box('a', size=1)\n")
    h.fake().reset()
    # size is immutable -> REPLACE; the identity (immutable size) changes, so CBD
    # is safe. Executor creates the new resource before deleting the old.
    report = h.apply("Box('a', size=2, lifecycle=Lifecycle(create_before_destroy=True))\n")
    assert report.replaced == [A]
    assert h.fake().calls == [("create", "a"), ("delete", "a")]  # create BEFORE delete
    node = h.backend.load().get(A)
    assert node is not None
    assert node.outputs == {"out": "a:2"}


def test_default_replace_is_destroy_before_create(tmp_path: object) -> None:
    h = Harness(MemoryStateBackend())
    h.apply("Box('a', size=1)\n")
    h.fake().reset()
    report = h.apply("Box('a', size=2)\n")  # no CBD
    assert report.replaced == [A]
    assert h.fake().calls == [("delete", "a"), ("create", "a")]  # delete BEFORE create


# -- collision guard (engine downgrades CBD -> DBC when identity is unchanged) --


def _engine() -> Engine:
    return engine_for(Server)


def _replace_change(*, name_desired: str, name_prior: str) -> Change:
    node_id = "default:test.Server:s"
    desired = IRNode(
        id=node_id,
        type=Server.type_name(),
        provider="test",
        provider_version="1.0.0",
        properties={"name": name_desired, "zone": "b"},
        dependencies=(),
        create_before_destroy=True,
    )
    prior = StateNode(
        id=node_id,
        type=Server.type_name(),
        provider="test",
        provider_version="1.0.0",
        input_hash="h",
        properties={"name": name_prior, "zone": "a"},
    )
    return Change(
        node_id=node_id,
        action=Action.REPLACE,
        desired=desired,
        prior=prior,
        changed_fields=("zone",),
        create_before_destroy=True,
    )


def test_cbd_downgraded_when_physical_name_unchanged() -> None:
    # zone changed but the physical name is the same -> the replacement would
    # collide with the old resource -> fall back to destroy-before-create.
    change = _replace_change(name_desired="web", name_prior="web")
    resolved, warnings = _engine()._planner._resolve_cbd(ChangeSet((change,)))
    assert resolved.changes[0].create_before_destroy is False
    assert len(warnings) == 1
    assert "create_before_destroy not possible" in warnings[0]


def test_cbd_kept_when_physical_name_changes() -> None:
    change = _replace_change(name_desired="web2", name_prior="web")
    resolved, warnings = _engine()._planner._resolve_cbd(ChangeSet((change,)))
    assert resolved.changes[0].create_before_destroy is True
    assert warnings == ()


# -- deploy round-trip preserves lifecycle -----------------------------------


def test_lifecycle_survives_ir_lowering() -> None:
    """A lowered node carries its lifecycle so a source-less deploy keeps it."""
    src = "Box('a', size=1, lifecycle=Lifecycle(ignore_changes=['label'], prevent_destroy=True))\n"
    registry = evaluate_source(src, extra_globals=GLOBALS).unwrap()
    ir = lower(registry)
    node = ir.node(A)
    assert node is not None
    assert node.ignore_changes == ("label",)
    assert node.prevent_destroy is True
