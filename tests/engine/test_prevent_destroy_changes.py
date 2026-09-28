"""A ``prevent_destroy`` change takes effect in the plan that makes it.

The flag is not part of the Merkle hash, so a protect-only edit diffs NOOP and
used to reach state only at the node's next real write: adding protection to
existing infrastructure guarded nothing, and removing it did not unlock
``destroy``. The model now matches Terraform's:

- for a node the config declares, the guard reads the **desired** flag, so a
  protect added in this plan already refuses a replace in this plan, and one
  removed in this plan already permits it;
- for a node the config drops (a DELETE, ``destroy``) the **stored** flag decides;
- a node whose only difference is the flag plans as a *state-only* NOOP, which
  the apply persists without calling its provider.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from atlantide.core import Lifecycle, PreventDestroyError
from atlantide.engine import Engine
from atlantide.reconcile import Action, restrict
from atlantide.state import MemoryStateBackend, SqliteStateBackend, StateBackend
from tests.support import Box, FakeProvider, box_harness, engine_for, globals_of, leaves

GLOBALS = globals_of(Box, Lifecycle=Lifecycle)
A = "default:test.Box:a"
B = "default:test.Box:b"

PLAIN = "Box('a', size=1)\n"
PROTECTED = "Box('a', size=1, lifecycle=Lifecycle(prevent_destroy=True))\n"
PROTECTED_RESIZED = "Box('a', size=2, lifecycle=Lifecycle(prevent_destroy=True))\n"


def _engine(backend: StateBackend | None = None) -> tuple[Engine, FakeProvider]:
    provider = FakeProvider()
    engine = engine_for(Box, provider=provider, backend=backend or MemoryStateBackend())
    return engine, provider


def _plan(engine: Engine, source: str) -> Any:
    return engine.plan(source, extra_globals=GLOBALS)


def _apply(engine: Engine, source: str) -> Any:
    return asyncio.run(engine.apply(source, extra_globals=GLOBALS))


# -- the diff ------------------------------------------------------------------


def test_a_protect_only_change_plans_a_state_only_noop() -> None:
    h = box_harness(MemoryStateBackend())
    h.apply(PLAIN)
    change = h.diff_only(PROTECTED).changes[0]
    assert change.action is Action.NOOP
    assert change.state_only is True
    assert change.changed_fields == ()


def test_an_unchanged_flag_is_a_plain_noop() -> None:
    h = box_harness(MemoryStateBackend())
    h.apply(PROTECTED)
    change = h.diff_only(PROTECTED).changes[0]
    assert change.action is Action.NOOP
    assert change.state_only is False


def test_a_state_only_change_is_pending_but_not_actionable() -> None:
    """``actionable`` stays "needs the provider"; ``pending`` is "an apply would
    change state", which is what `--detailed-exitcode` and the apply prompt ask."""
    h = box_harness(MemoryStateBackend())
    h.apply(PLAIN)
    changeset = h.diff_only(PROTECTED)
    assert changeset.actionable == []
    assert [c.node_id for c in changeset.pending] == [A]


def test_an_untargeted_state_only_change_is_dropped() -> None:
    """``--target`` leaves untargeted rows bit-for-bit, the flag included."""
    h = box_harness(MemoryStateBackend())
    h.apply(PLAIN)
    narrowed = restrict(h.diff_only(PROTECTED), frozenset())
    assert narrowed.pending == []


def test_the_fingerprint_covers_a_state_only_change() -> None:
    """An apply approved without a flag change must not silently persist one."""
    h = box_harness(MemoryStateBackend())
    h.apply(PLAIN)
    assert h.diff_only(PROTECTED).fingerprint() != h.diff_only(PLAIN).fingerprint()


# -- the guard reads the desired flag for declared nodes -----------------------


def test_protect_added_in_the_same_plan_refuses_a_replace() -> None:
    engine, _ = _engine()
    _apply(engine, PLAIN).unwrap()
    planned = _plan(engine, PROTECTED_RESIZED)
    assert isinstance(planned.failure(), PreventDestroyError)


def test_protect_removed_in_the_same_plan_permits_a_replace() -> None:
    engine, provider = _engine()
    _apply(engine, PROTECTED).unwrap()
    provider.reset()
    planned = _plan(engine, "Box('a', size=2)\n").unwrap()
    assert planned.changeset.changes[0].action is Action.REPLACE
    report = _apply(engine, "Box('a', size=2)\n").unwrap()
    assert report.replaced == [A]
    assert provider.calls == [("delete", "a"), ("create", "a")]


def test_a_dropped_node_is_judged_by_its_stored_flag() -> None:
    """Config no longer declares it, so state is the only record of the flag."""
    engine, _ = _engine()
    _apply(engine, PROTECTED + "Box('b', size=1)\n").unwrap()
    planned = _plan(engine, "Box('b', size=1)\n")
    assert isinstance(planned.failure(), PreventDestroyError)


# -- the apply persists the flag, and only the flag ------------------------------


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_adding_protect_persists_without_a_provider_call(kind: str, tmp_path: Any) -> None:
    backend: StateBackend = (
        MemoryStateBackend() if kind == "memory" else SqliteStateBackend(str(tmp_path / "s.db"))
    )
    engine, provider = _engine(backend)
    _apply(engine, PLAIN).unwrap()
    before = backend.load().nodes[A]
    provider.reset()

    report = _apply(engine, PROTECTED).unwrap()

    assert provider.calls == []  # Merkle skip still skips the provider
    assert report.noop == [A]
    assert report.state_only == [A]
    after = backend.load().nodes[A]
    assert after.prevent_destroy is True
    # Nothing but the flag moved: same hash, outputs, properties.
    assert after == replace(before, prevent_destroy=True)
    # And the next plan is clean.
    assert _plan(engine, PROTECTED).unwrap().changeset.pending == []


def test_destroy_is_refused_once_the_flag_is_applied_and_allowed_once_removed() -> None:
    engine, provider = _engine()
    _apply(engine, PLAIN).unwrap()
    _apply(engine, PROTECTED).unwrap()
    refused = asyncio.run(engine.destroy())
    assert isinstance(refused.failure(), PreventDestroyError)
    assert engine.backend.load().nodes[A].prevent_destroy is True

    provider.reset()
    report = _apply(engine, PLAIN).unwrap()
    assert report.state_only == [A]
    assert provider.calls == []
    assert engine.backend.load().nodes[A].prevent_destroy is False

    destroyed = asyncio.run(engine.destroy()).unwrap()
    assert destroyed.deleted == [A]


def test_the_state_only_write_is_reported_as_events() -> None:
    engine, _ = _engine()
    _apply(engine, PLAIN).unwrap()
    seen: list[Any] = []
    engine.events = seen.append
    _apply(engine, PROTECTED).unwrap()
    node_events = [e for e in seen if getattr(e, "node_id", None) == A]
    assert [(e.phase, e.action) for e in node_events] == [
        ("node_start", "noop"),
        ("node_finish", "noop"),
    ]
    assert all(e.detail.get("state_only") is True for e in node_events)


def test_a_rollback_restores_the_previous_flag() -> None:
    """The state-only write is part of the run, so the saga undoes it too."""
    h = box_harness(MemoryStateBackend())
    h.apply("a = Box('a', size=1)\nBox('b', size=1, ref=a.out)\n")
    h.fake().fail_update.add("b")
    with pytest.raises(ExceptionGroup) as failed:
        h.apply(
            "a = Box('a', size=1, lifecycle=Lifecycle(prevent_destroy=True))\n"
            "Box('b', size=1, label='y', ref=a.out)\n",
            on_failure="rollback",
        )
    assert any("update failed for b" in str(leaf) for leaf in leaves(failed.value))
    row = h.backend.load().nodes[A]
    assert row.prevent_destroy is False
    # The row is back as it was, so the next plan re-offers the flag change.
    assert (
        h.diff_only(
            "a = Box('a', size=1, lifecycle=Lifecycle(prevent_destroy=True))\n"
            "Box('b', size=1, ref=a.out)\n"
        )
        .changes[0]
        .state_only
    )
