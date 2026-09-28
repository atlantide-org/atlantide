"""Regressions: diff scaling, targeted replace, alias rehash, refresh writes, adopt reads."""

from __future__ import annotations

import asyncio
import itertools
import threading
import time
from dataclasses import replace
from typing import Any

import pytest

from atlantide.core import Lifecycle
from atlantide.core.actions import Action
from atlantide.core.errors import LeaseLostError
from atlantide.ir.model import IRGraph, IRNode
from atlantide.reconcile.adopt import ImportStatus
from atlantide.reconcile.aliases import resolve_aliases
from atlantide.reconcile.changes import restrict
from atlantide.reconcile.diff import diff
from atlantide.reconcile.env import Desired
from atlantide.reconcile.executor import apply
from atlantide.reconcile.writer import state_writer
from atlantide.state import NO_INPUT_HASH, MemoryStateBackend, StateGraph, StateNode
from tests.support import FakeProvider, Harness, Widget, state_node
from tests.support.resources import Box

A = "default:test.Box:a"
B = "default:test.Box:b"

# -- diff scaling -------------------------------------------------------------


def test_a_wide_fan_out_diffs_in_linear_time() -> None:
    """16k dependents of one changed node. Each used to copy the whole moved set
    (O(n * |moved|): ~2.5 s here); linear, it takes ~0.1 s."""
    root = IRNode(
        id="r", type="T", provider="p", provider_version="", properties={"a": 1}, dependencies=()
    )
    nodes = [root] + [
        IRNode(
            id=f"n{i}",
            type="T",
            provider="p",
            provider_version="",
            properties={"k": {"$ref": "r#x"}},
            dependencies=("r",),
        )
        for i in range(16_000)
    ]
    prior = StateGraph(
        nodes={
            n.id: StateNode(
                id=n.id,
                type="T",
                provider="p",
                provider_version="",
                input_hash="old",
                outputs={},
                properties=dict(n.properties),
                dependencies=n.dependencies,
            )
            for n in nodes
        }
    )
    started = time.perf_counter()
    changes = diff(IRGraph(nodes=tuple(nodes)), {n.id: "new" for n in nodes}, prior, {})
    elapsed = time.perf_counter() - started

    assert elapsed < 1.5
    assert {c.changed_fields for c in changes if c.node_id != "r"} == {("k",)}


# -- a targeted replace -------------------------------------------------------


def test_a_dependent_left_out_of_a_targeted_replace_is_replanned_next_time() -> None:
    """`--target a --replace a` NOOPs `b`, which keeps the old `a` output. Its
    row records what its ref consumed, so the next full plan re-applies it."""
    serial = itertools.count(1)
    provider = FakeProvider(
        on_create=lambda ctx, res: {"out": f"{res.logical_name}#{next(serial)}"}
    )
    h = Harness.of(Box, provider=provider)
    source = "a = Box('a', size=1)\nBox('b', size=2, ref=a.out)\n"
    h.apply(source)
    providers = h._providers()
    registry, ir, graph, hashes = h._compile(source, providers)
    prior = h.backend.load()
    targeted = restrict(
        diff(ir, hashes, prior, h._mutability(), replace=frozenset({A})), frozenset({A})
    )
    desired = Desired(
        ir=ir, graph=graph, hashes=hashes, resources={r.node_id: r for r in registry.all()}
    )
    asyncio.run(apply(changeset=targeted, desired=desired, prior=prior, env=h._env(providers)))

    [update] = [c for c in h.diff_only(source) if c.node_id == B]
    assert update.action is Action.UPDATE
    assert update.upstream_moved == ("ref",)


# -- alias migration ----------------------------------------------------------


def test_an_alias_migration_keeps_an_unrelated_stale_mark() -> None:
    """Re-hashing migrated state must not clear a `refresh --write` poison on a
    node the rename does not touch: the drift would vanish from the next plan."""
    h = Harness.of(Box, globals={"Lifecycle": Lifecycle})
    h.apply("Box('a', size=1)\nBox('b', size=2)\n")
    h.backend.put(replace(h.backend.load().nodes[A], input_hash=NO_INPUT_HASH))
    renamed = "Box('a', size=1)\nBox('c', size=2, lifecycle=Lifecycle(aliases=['b']))\n"
    _, ir, _, _ = h._compile(renamed, h._providers())

    migrated, remap = resolve_aliases(h.backend.load(), ir)

    assert remap == {B: "default:test.Box:c"}
    assert migrated.nodes[A].input_hash == NO_INPUT_HASH
    assert migrated.nodes["default:test.Box:c"].input_hash != NO_INPUT_HASH


# -- refresh --write ----------------------------------------------------------


class _Recording(MemoryStateBackend):
    def __init__(self) -> None:
        super().__init__()
        self.writes: list[tuple[str, str]] = []

    def put(self, node: StateNode) -> None:
        self.writes.append((node.id, threading.current_thread().name))
        super().put(node)


def _seeded(**live: dict[str, Any] | None) -> tuple[_Recording, Harness]:
    backend = _Recording()
    for name in ("a", "b"):
        backend.put(
            state_node(
                name, type=Widget.type_name(), outputs={"arn": name}, properties={"label": name}
            )
        )
    backend.writes.clear()
    return backend, Harness.of(Widget, provider=FakeProvider(live=live), backend=backend)


def test_refresh_write_skips_rows_without_drift() -> None:
    backend, h = _seeded(a={"arn": "a", "label": "a"}, b={"arn": "b", "label": "edited"})

    h.refresh(write=True)

    assert [node_id.rsplit(":", 1)[-1] for node_id, _ in backend.writes] == ["b"]


def test_refresh_write_goes_through_the_state_writer() -> None:
    backend, h = _seeded(a={"arn": "a", "label": "x"}, b={"arn": "b", "label": "y"})

    with state_writer(backend, offload=True):
        h.refresh(write=True)

    assert len(backend.writes) == 2
    assert all(thread.startswith("atlantide-state") for _, thread in backend.writes)


def test_refresh_write_refuses_to_write_without_the_lease() -> None:
    backend, h = _seeded(a={"arn": "a", "label": "x"}, b={"arn": "b", "label": "b"})
    h.lease.fail(LeaseLostError("lease lost"))

    with pytest.raises(ExceptionGroup) as raised:
        h.refresh(write=True)

    assert raised.group_contains(LeaseLostError)
    assert backend.writes == []


# -- adopt --------------------------------------------------------------------


def test_a_failed_read_blocks_only_its_own_import() -> None:
    h = Harness.of(Box, provider=FakeProvider(live={"a": {"out": "a"}, "b": {"out": "b"}}))
    h.fake().fail_read.add("a")

    outcomes = h.adopt("Box('a', size=1)\nBox('b', size=2)\n", A, B)

    assert [o.status for o in outcomes] == [ImportStatus.BLOCKED, ImportStatus.IMPORTED]
    assert "read failed" in outcomes[0].detail
    assert set(h.backend.load().nodes) == {B}


# -- depends_on on state rows ------------------------------------------------


def test_an_alias_rename_carries_the_depends_on_edge() -> None:
    """A rename of the upstream must keep the dependent's ordering edge on it."""
    h = Harness.of(Box, globals={"Lifecycle": Lifecycle})
    h.apply("a = Box('a', size=1)\nBox('d', size=1, depends_on=[a])\n")
    renamed = (
        "a = Box('z', size=1, lifecycle=Lifecycle(aliases=['a']))\n"
        "Box('d', size=1, depends_on=[a])\n"
    )
    _, ir, _, _ = h._compile(renamed, h._providers())

    migrated, remap = resolve_aliases(h.backend.load(), ir)

    assert remap == {A: "default:test.Box:z"}
    assert migrated.nodes["default:test.Box:d"].depends_on == ("default:test.Box:z",)


def test_an_adopted_row_records_depends_on() -> None:
    source = "a = Box('a', size=1)\nBox('d', size=1, depends_on=[a])\n"
    h = Harness.of(Box, provider=FakeProvider(live={"a": {"out": "a"}, "d": {"out": "d"}}))

    h.adopt(source, A, "default:test.Box:d")

    assert h.backend.load().nodes["default:test.Box:d"].depends_on == (A,)
