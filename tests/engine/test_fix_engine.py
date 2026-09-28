"""Regressions for the engine fix round: artifact envs, lock scope, renewal, re-reads.

Each test names the defect it pins in its docstring.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Set
from pathlib import Path
from typing import Any

import pytest
from returns.result import Failure, Result, Success

from atlantide.core.errors import (
    ArtifactError,
    LeaseLostError,
    LockError,
    PlanDriftError,
    StateError,
)
from atlantide.core.events import LEASE_LOST, LEASE_RENEW, ApplyEvent
from atlantide.core.lifecycle import Lifecycle
from atlantide.engine.compiler import compiled_from_artifact
from atlantide.engine.locking import apply_scope, with_lock
from atlantide.engine.planner import raise_drift
from atlantide.ir import artifact as artifact_module
from atlantide.reconcile import Action, Change, ChangeSet
from atlantide.state import Lease, LockPolicy, MemoryStateBackend, StateBackend, StateGraph
from atlantide.state.sql.sqlite import SqliteStateBackend
from tests.support import Box, SpyBackend, engine_for, globals_of, state_node

GLOBALS = globals_of(Box)

ENVS = (
    "from atlantide.core import Config, Stack\n"
    "config = Config(envs={'dev': {'region': 'r1'}, 'prod': {'region': 'r2'}})\n"
    "for env in config.envs():\n"
    "    with Stack(env.name, config=env):\n"
    "        Box('a', size=1)\n"
)

FAST = LockPolicy(ttl=0.30, renew_interval=0.02, renew_grace=0.0)
SCOPE = frozenset({"default:test.Box:a"})


# -- 1. an artifact built under --env narrows its deploy ------------------------


async def test_a_narrowed_artifact_deploy_does_not_delete_the_other_environment() -> None:
    """``build --env prod`` then ``deploy``: dev's rows must not plan as DELETE."""
    engine = engine_for(Box, backend=MemoryStateBackend())
    (await engine.apply(ENVS, extra_globals=GLOBALS)).unwrap()
    assert set(engine.backend.load().nodes) == {"dev:test.Box:a", "prod:test.Box:a"}

    built = engine.build(ENVS, envs=["prod"], extra_globals=GLOBALS).unwrap()
    artifact = artifact_module.loads(built.dumps()).unwrap()  # through the file format
    compiled = compiled_from_artifact(artifact, engine.types).unwrap()
    assert compiled.envs_excluded == ("dev",)

    report = (await engine.deploy(artifact)).unwrap()
    assert report.deleted == []
    assert "dev:test.Box:a" in engine.backend.load().nodes


def test_an_artifact_without_declared_envs_still_loads() -> None:
    """Artifacts written before ``envs_declared`` existed load, narrowing nothing."""
    engine = engine_for(Box)
    built = engine.build("Box('a', size=1)\n", extra_globals=GLOBALS).unwrap()
    data = json.loads(built.dumps())
    del data["envs_declared"]

    loaded = artifact_module.loads(json.dumps(data)).unwrap()
    assert loaded.envs_declared == ()
    assert compiled_from_artifact(loaded, engine.types).unwrap().envs_excluded == ()


async def test_an_old_narrowed_artifact_is_refused_rather_than_deleting_other_envs() -> None:
    """Built with ``--env`` before ``envs_declared`` existed: the excluded
    environments are unknown, so deploying would plan their rows as DELETE."""
    engine = engine_for(Box, backend=MemoryStateBackend())
    (await engine.apply(ENVS, extra_globals=GLOBALS)).unwrap()
    data = json.loads(engine.build(ENVS, envs=["prod"], extra_globals=GLOBALS).unwrap().dumps())
    del data["envs_declared"]
    old = artifact_module.loads(json.dumps(data)).unwrap()

    refused = compiled_from_artifact(old, engine.types)
    assert isinstance(refused, Failure)
    assert isinstance(refused.failure(), ArtifactError)
    assert "rebuild it with `atlantide build`" in str(refused.failure())

    deployed = await engine.deploy(old)
    assert isinstance(deployed, Failure)
    assert set(engine.backend.load().nodes) == {"dev:test.Box:a", "prod:test.Box:a"}


# -- 2. the apply lock scope ---------------------------------------------------


def test_the_apply_scope_is_the_desired_graph_plus_state() -> None:
    backend = MemoryStateBackend()
    backend.put(state_node("stray", type="test.Box", properties={"size": 9}))
    engine = engine_for(Box, backend=backend)
    source = "a = Box('a', size=1)\nBox('b', size=2, ref=a.out)\n"
    plan = engine.plan(source, extra_globals=GLOBALS).unwrap()
    prior = backend.load()

    assert apply_scope(plan, prior) == frozenset(
        {"default:test.Box:a", "default:test.Box:b", "default:test.Box:stray"}
    )


# -- 3. a raising event sink does not stop renewal -------------------------------


def _raising_on(kind: str) -> Any:
    def sink(event: ApplyEvent) -> None:
        if event.phase == kind:
            raise RuntimeError(f"sink refused {kind}")

    return sink


async def test_a_sink_raising_on_renew_keeps_renewing_and_keeps_the_result() -> None:
    backend = SpyBackend()

    async def slow() -> str:
        await asyncio.sleep(FAST.renew_interval * 4.5)
        return "done"

    result = await with_lock(backend, SCOPE, slow, policy=FAST, events=_raising_on(LEASE_RENEW))

    assert isinstance(result, Success), result
    assert result.unwrap() == "done"
    assert backend.count("renew_lock") >= 3, backend.names()


async def test_a_sink_raising_on_lease_lost_still_cancels_the_run() -> None:
    backend = SpyBackend(fail_lock_after=1)

    async def slow() -> str:
        await asyncio.sleep(FAST.renew_interval * 20)
        return "should not get here"

    result = await with_lock(backend, SCOPE, slow, policy=FAST, events=_raising_on(LEASE_LOST))

    assert isinstance(result, Failure)
    assert isinstance(result.failure(), LeaseLostError)


# -- 4. an apply refuses rows created while it waited for the lock ---------------


class _RacedBackend(MemoryStateBackend):
    """Once armed, a row lands just before the next lock is granted."""

    armed = False

    def acquire_lock(self, owner: str, ttl: float, nodes: Set[str]) -> Result[Lease, LockError]:
        if self.armed:
            self.armed = False
            self.put(state_node("late", type="test.Box", properties={"size": 2}))
        return super().acquire_lock(owner, ttl, nodes)


async def test_an_apply_refuses_rows_created_while_it_waited_for_the_lock() -> None:
    backend = _RacedBackend()
    engine = engine_for(Box, backend=backend)
    backend.armed = True

    with pytest.raises(StateError) as caught:
        await engine.apply("Box('a', size=1)\n", extra_globals=GLOBALS)

    assert "while apply waited for the lock: default:test.Box:late" in str(caught.value)
    assert "default:test.Box:late" in backend.load().nodes, "the row was not deleted"


# -- 5. one apply reads state twice, not four times ------------------------------


class _CountingBackend(MemoryStateBackend):
    loads = 0

    def load(self) -> StateGraph:
        self.loads += 1
        return super().load()


async def test_an_apply_reads_state_once_before_and_once_under_the_lock() -> None:
    backend = _CountingBackend()
    engine = engine_for(Box, backend=backend)

    (await engine.apply("Box('a', size=1)\n", extra_globals=GLOBALS)).unwrap()

    assert backend.loads == 2


# -- 6. concurrent runs on one backend instance ----------------------------------


async def test_two_runs_on_one_backend_instance_do_not_overlap() -> None:
    """Disjoint configs lock disjoint scopes, but one backend holds one bound lease
    and one writer: the second run must refuse rather than rebind the first's."""
    backend = MemoryStateBackend()
    first = engine_for(Box, backend=backend)
    second = engine_for(Box, backend=backend)

    results = await asyncio.gather(
        first.apply("Box('a', size=1)\n", extra_globals=GLOBALS),
        second.apply("Box('b', size=2)\n", extra_globals=GLOBALS),
    )

    succeeded = [r for r in results if isinstance(r, Success)]
    refused = [r.failure() for r in results if isinstance(r, Failure)]
    assert len(succeeded) == 1 and len(refused) == 1, results
    assert isinstance(refused[0], LockError)
    assert "already running" in str(refused[0])
    # The refused run left nothing behind: a later run on the backend proceeds.
    assert isinstance(await second.apply("Box('b', size=2)\n", extra_globals=GLOBALS), Success)


# -- 7. drift messages name what differs -----------------------------------------


def test_a_field_only_drift_names_the_fields() -> None:
    approved = ChangeSet(changes=(Change("x", Action.UPDATE, changed_fields=("size",)),))
    fresh = ChangeSet(changes=(Change("x", Action.UPDATE, changed_fields=("label",)),))

    with pytest.raises(PlanDriftError) as caught:
        raise_drift(approved, fresh)

    message = str(caught.value)
    assert "update x [label]" in message
    assert "update x [size]" in message


def test_a_cbd_only_drift_says_so() -> None:
    approved = ChangeSet(changes=(Change("x", Action.REPLACE, changed_fields=("size",)),))
    fresh = ChangeSet(
        changes=(Change("x", Action.REPLACE, changed_fields=("size",), create_before_destroy=True),)
    )

    with pytest.raises(PlanDriftError) as caught:
        raise_drift(approved, fresh)

    assert "replace x [size] create-before-destroy" in str(caught.value)


# -- create-before-destroy companions are inside the apply's lock scope ---------

CBD_GLOBALS = globals_of(Box, Lifecycle=Lifecycle)


def _cbd_box(size: int) -> str:
    return f"Box('a', size={size}, lifecycle=Lifecycle(create_before_destroy=True))\n"


@pytest.fixture(params=["memory", "sqlite"])
def any_backend(request: pytest.FixtureRequest, tmp_path: Path) -> StateBackend:
    if request.param == "memory":
        return MemoryStateBackend()
    return SqliteStateBackend(str(tmp_path / "s.db"))


async def test_a_create_before_destroy_replace_runs_under_the_real_lock(
    any_backend: StateBackend,
) -> None:
    """The executor writes ``a~replaced`` before creating the replacement; outside
    the lease that write is refused as unfenced."""
    engine = engine_for(Box, backend=any_backend)
    (await engine.apply(_cbd_box(1), extra_globals=CBD_GLOBALS)).unwrap()

    report = (await engine.apply(_cbd_box(2), extra_globals=CBD_GLOBALS)).unwrap()

    assert report.replaced == ["default:test.Box:a"]
    assert set(any_backend.load().nodes) == {"default:test.Box:a"}


def test_only_create_before_destroy_nodes_widen_the_scope() -> None:
    engine = engine_for(Box)
    source = _cbd_box(1) + "Box('b', size=1)\n"
    plan = engine.plan(source, extra_globals=CBD_GLOBALS).unwrap()

    assert apply_scope(plan, StateGraph(nodes={})) == frozenset(
        {"default:test.Box:a", "default:test.Box:a~replaced", "default:test.Box:b"}
    )


async def test_a_leftover_companion_row_is_in_scope_for_the_apply_that_deletes_it() -> None:
    """A companion whose cleanup failed is a state row, so it is in the scope sized
    from state and the apply that destroys it neither refuses nor is fenced."""
    backend = MemoryStateBackend()
    engine = engine_for(Box, backend=backend)
    (await engine.apply("Box('a', size=1)\n", extra_globals=GLOBALS)).unwrap()
    stored = backend.load().nodes["default:test.Box:a"]
    backend.put(dataclasses.replace(stored, id="default:test.Box:a~replaced"))

    report = (await engine.apply("Box('a', size=1)\n", extra_globals=GLOBALS)).unwrap()

    assert report.deleted == ["default:test.Box:a~replaced"]
    assert set(backend.load().nodes) == {"default:test.Box:a"}
