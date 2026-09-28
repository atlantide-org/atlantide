"""The engine's small shared pieces: the ``Result`` bridge, the gained-nodes
refusal, and the value helpers the façade builds runs from."""

from __future__ import annotations

from collections.abc import Set

import pytest
from returns.result import Failure, Result, Success

from atlantide.core.errors import AtlantideError, LockError, StateError
from atlantide.engine.locking import require_no_new_nodes
from atlantide.engine.result import catching
from atlantide.policy import PolicyContext, PolicyRegistry, PolicyResult
from atlantide.reconcile import Desired
from atlantide.state import Lease, MemoryStateBackend, StateGraph
from tests.support import Box, engine_for, globals_of, state_node

GLOBALS = globals_of(Box)
A = "default:test.Box:a"
LATE = "default:test.Box:late"


def test_catching_wraps_a_value_in_success() -> None:
    assert catching(lambda: 42) == Success(42)


def test_catching_turns_an_atlantide_error_into_a_failure() -> None:
    error = StateError("boom")

    def fail() -> int:
        raise error

    result = catching(fail)
    assert isinstance(result, Failure)
    assert result.failure() is error


def test_catching_lets_any_other_exception_through() -> None:
    """Only the domain error crosses into ``Result``; a bug keeps raising."""

    def bug() -> int:
        raise KeyError("not a domain error")

    with pytest.raises(KeyError):
        catching(bug)


def _graph(*node_ids: str) -> StateGraph:
    return StateGraph(
        nodes={
            node_id: state_node(node_id.rsplit(":", 1)[1], type="test.Box") for node_id in node_ids
        }
    )


def test_no_new_nodes_is_silent_when_state_stayed_inside_the_scope() -> None:
    require_no_new_nodes(_graph(A), frozenset({A, LATE}), "backup", "re-run backup")


def test_new_nodes_are_refused_with_the_command_and_the_remedy() -> None:
    with pytest.raises(StateError) as caught:
        require_no_new_nodes(_graph(A, LATE, "default:test.Box:b"), {A}, "backup", "re-run backup")
    assert str(caught.value) == (
        "state gained node(s) while backup waited for the lock: "
        "default:test.Box:b, default:test.Box:late — re-run backup"
    )


class _RacedBackend(MemoryStateBackend):
    """Once armed, a row lands just before the next lock is granted, as if written
    by a run that held the lock while this one waited."""

    armed = False

    def acquire_lock(self, owner: str, ttl: float, nodes: Set[str]) -> Result[Lease, LockError]:
        if self.armed:
            self.armed = False
            self.put(state_node("late", type="test.Box", properties={"size": 2}))
        return super().acquire_lock(owner, ttl, nodes)


async def test_a_destroy_refuses_rows_created_while_it_waited_for_the_lock() -> None:
    backend = _RacedBackend()
    engine = engine_for(Box, backend=backend)
    (await engine.apply("Box('a', size=1)\n", extra_globals=GLOBALS)).unwrap()
    backend.armed = True

    # Raised, not returned: the refusal happens inside the lock, past the
    # ``Result`` layer (see the engine README's error model).
    with pytest.raises(StateError) as caught:
        await engine.destroy()

    assert str(caught.value) == (
        "state gained node(s) while destroy waited for the lock: "
        f"{LATE} — re-run destroy to include them"
    )
    assert A in engine.backend.load().nodes, "nothing was destroyed"


def test_an_empty_desired_config_declares_nothing() -> None:
    empty = Desired.empty()
    assert empty.ir.nodes == ()
    assert empty.graph.node_ids == ()
    assert (empty.hashes, empty.resources, empty.output_decls) == ({}, {}, {})


def test_a_compiled_config_hands_the_executor_its_parts() -> None:
    engine = engine_for(Box)
    compiled = engine.compile(
        "from atlantide.core import output\nBox('a', size=1)\noutput('size', 1)\n",
        extra_globals=GLOBALS,
    ).unwrap()
    desired = compiled.desired()
    assert desired.ir is compiled.ir
    assert desired.graph is compiled.graph
    assert desired.hashes is compiled.hashes
    assert desired.resources is compiled.resources
    assert desired.output_decls is compiled.outputs


class _UnreachablePolicies(PolicyRegistry):
    def evaluate(self, name: str, ctx: PolicyContext) -> PolicyResult:
        raise AtlantideError("policy backend unreachable")


def test_an_error_raised_by_a_policy_becomes_a_failed_plan() -> None:
    """The planner's policy pass raises; ``plan`` must still return a ``Failure``."""
    engine = engine_for(Box, policies=_UnreachablePolicies())
    source = "from atlantide.policy import enforce\nenforce('anything')\nBox('a', size=1)\n"
    result = engine.plan(source, extra_globals=GLOBALS)
    assert isinstance(result, Failure)
    assert str(result.failure()) == "policy backend unreachable"
