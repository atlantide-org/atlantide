"""``Engine.import_nodes``: the library entry to adopting existing resources.

The per-node rules live in ``tests/reconcile/test_adopt.py``; this covers what
the façade adds — ordering, the three flags reaching the batch, and the lock
being taken only for a write.
"""

from __future__ import annotations

from typing import Any

from atlantide.engine import Engine
from atlantide.reconcile import Action, ImportRequest
from atlantide.reconcile.adopt import ImportStatus
from atlantide.state import MemoryStateBackend
from tests.support import Box, FakeProvider, engine_for, globals_of

GLOBALS = globals_of(Box)
SOURCE = "Box('b', size=1, label='hi')\n"
BOX_ID = "default:test.Box:b"


def _engine(live: dict[str, dict[str, Any] | None]) -> Engine:
    return engine_for(Box, provider=FakeProvider(live=live), backend=MemoryStateBackend())


async def _import(engine: Engine, **flags: bool) -> ImportStatus:
    compiled = engine.compile(SOURCE, extra_globals=GLOBALS).unwrap()
    [outcome] = (await engine.import_nodes(compiled, [ImportRequest(BOX_ID)], **flags)).unwrap()
    return outcome.status


async def test_a_dry_run_writes_nothing() -> None:
    engine = _engine({"b": {"out": "b:1"}})
    assert await _import(engine, write=False) is ImportStatus.WOULD_IMPORT
    assert engine.backend.load().nodes == {}


async def test_an_import_then_plans_as_a_noop() -> None:
    engine = _engine({"b": {"out": "b:1"}})
    assert await _import(engine) is ImportStatus.IMPORTED
    plan = engine.plan(SOURCE, extra_globals=GLOBALS).unwrap()
    assert [change.action for change in plan.changeset] == [Action.NOOP]


async def test_force_and_allow_drift_reach_the_batch() -> None:
    engine = _engine({"b": {"out": "b:1", "label": "changed"}})
    assert await _import(engine) is ImportStatus.DRIFTED
    assert await _import(engine, allow_drift=True) is ImportStatus.IMPORTED
    assert await _import(engine, allow_drift=True) is ImportStatus.ALREADY_TRACKED
    assert await _import(engine, allow_drift=True, force=True) is ImportStatus.IMPORTED


async def test_an_empty_batch_adopts_nothing() -> None:
    engine = _engine({})
    compiled = engine.compile(SOURCE, extra_globals=GLOBALS).unwrap()
    assert (await engine.import_nodes(compiled, [])).unwrap() == []
