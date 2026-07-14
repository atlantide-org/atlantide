"""Backend-parametrized fixtures: every state test runs on memory AND sqlite."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from atlantide.state import MemoryStateBackend, SqliteStateBackend, StateBackend
from tests.support import FakeClock

__all__ = ["BackendFactory", "FakeClock", "make_backend"]

BackendFactory = Callable[..., StateBackend]


@pytest.fixture(params=["memory", "sqlite"])
def make_backend(request: pytest.FixtureRequest, tmp_path: Any) -> Iterator[BackendFactory]:
    created: list[StateBackend] = []

    def factory(clock: Callable[[], float] = time.time) -> StateBackend:
        if request.param == "memory":
            backend: StateBackend = MemoryStateBackend(clock=clock)
        else:
            backend = SqliteStateBackend(str(tmp_path / f"state{len(created)}.db"), clock=clock)
        created.append(backend)
        return backend

    yield factory
    for backend in created:
        backend.close()
