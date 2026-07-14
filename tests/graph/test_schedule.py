"""Async scheduler: dependency order, real concurrency, cap, reverse, halt."""

from __future__ import annotations

import asyncio

import pytest

from atlantide.graph import build_graph, run_graph

from .conftest import ir_from


async def test_runs_all_nodes() -> None:
    graph = build_graph(ir_from({"a": [], "b": ["a"], "c": ["b"]})).unwrap()
    results = await run_graph(graph, lambda n: _identity(n))
    assert results == {"a": "a", "b": "b", "c": "c"}


async def _identity(node_id: str) -> str:
    return node_id


async def test_respects_dependency_order() -> None:
    graph = build_graph(ir_from({"a": [], "b": ["a"], "c": ["b"]})).unwrap()
    starts: list[str] = []

    async def work(node_id: str) -> str:
        starts.append(node_id)
        await asyncio.sleep(0)
        return node_id

    await run_graph(graph, work)
    assert starts == ["a", "b", "c"]


async def test_independent_nodes_run_concurrently() -> None:
    # Three independent nodes must be in-flight together, or the barrier hangs.
    graph = build_graph(ir_from({"a": [], "b": [], "c": []})).unwrap()
    barrier = asyncio.Barrier(3)

    async def work(node_id: str) -> str:
        await asyncio.wait_for(barrier.wait(), timeout=1.0)
        return node_id

    results = await run_graph(graph, work, parallelism=3)
    assert set(results) == {"a", "b", "c"}


async def test_parallelism_cap_is_honoured() -> None:
    graph = build_graph(ir_from({f"n{i}": [] for i in range(6)})).unwrap()
    active = 0
    peak = 0

    async def work(node_id: str) -> str:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return node_id

    await run_graph(graph, work, parallelism=2)
    assert peak <= 2


async def test_reverse_order_for_destroy() -> None:
    graph = build_graph(ir_from({"a": [], "b": ["a"], "c": ["b"]})).unwrap()
    starts: list[str] = []

    async def work(node_id: str) -> str:
        starts.append(node_id)
        await asyncio.sleep(0)
        return node_id

    await run_graph(graph, work, reverse=True)
    assert starts == ["c", "b", "a"]


async def test_halt_on_failure_cancels_and_raises() -> None:
    graph = build_graph(ir_from({"a": [], "b": ["a"], "c": ["b"]})).unwrap()
    ran: list[str] = []

    async def work(node_id: str) -> str:
        ran.append(node_id)
        if node_id == "b":
            raise RuntimeError("boom")
        return node_id

    with pytest.raises(ExceptionGroup) as exc_info:
        await run_graph(graph, work)
    assert any(isinstance(e, RuntimeError) for e in exc_info.value.exceptions)
    # 'c' depends on 'b', so it must never have started.
    assert "c" not in ran


async def test_rejects_bad_parallelism() -> None:
    graph = build_graph(ir_from({"a": []})).unwrap()
    with pytest.raises(ValueError, match="parallelism"):
        await run_graph(graph, _identity, parallelism=0)
