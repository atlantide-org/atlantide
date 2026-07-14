"""Async DAG scheduler: run per-node work respecting dependency order.

Each node waits on its predecessors' completion events, then runs under a shared
semaphore. Independent subgraphs run concurrently up to ``parallelism``.
``reverse=True`` schedules destroy order (dependents first).

Halt-on-failure: the first work error cancels the remaining tasks
(``asyncio.TaskGroup`` semantics).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import TypeVar

from atlantide.graph.model import DiGraph

T = TypeVar("T")

DEFAULT_PARALLELISM = min(32, (os.cpu_count() or 4) * 4)


async def run_graph(
    graph: DiGraph,
    work: Callable[[str], Awaitable[T]],
    *,
    parallelism: int = DEFAULT_PARALLELISM,
    reverse: bool = False,
) -> dict[str, T]:
    """Run ``work(node_id)`` across the graph, honouring dependencies.

    Returns a mapping of node id -> work result. Raises (via ``ExceptionGroup``)
    if any work fails, after cancelling still-running siblings.
    """
    if parallelism < 1:
        raise ValueError("parallelism must be >= 1")

    done: dict[str, asyncio.Event] = {nid: asyncio.Event() for nid in graph.node_ids}
    results: dict[str, T] = {}
    semaphore = asyncio.Semaphore(parallelism)

    async def run_node(node_id: str) -> None:
        for pred in graph.predecessors(node_id, reverse=reverse):
            await done[pred].wait()
        async with semaphore:
            results[node_id] = await work(node_id)
        done[node_id].set()

    async with asyncio.TaskGroup() as tg:
        for node_id in graph.node_ids:
            tg.create_task(run_node(node_id))

    return results
