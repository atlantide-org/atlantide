"""CLI apply plumbing that exists for throughput.

``apply`` evaluates the config once — the plan it shows is the one it applies —
and the event loop's default executor is sized for ``--parallelism``, so the
thread pool behind ``asyncio.to_thread`` does not cap concurrent provider calls.
"""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from typing import Any

import pytest
from returns.result import Result, Success

from atlantide.cli.errors import run_async
from atlantide.core import AtlantideError
from atlantide.core.tuning import io_workers
from atlantide.engine import Engine
from tests.support import Cli

cli = Cli()


def test_apply_compiles_the_config_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "config.py"
    cfg.write_text(
        "from atlantide.providers.local import File\n"
        f"a = File('a', path={str(tmp_path / 'a.txt')!r}, content='one')\n"
        f"File('b', path={str(tmp_path / 'b.txt')!r}, content=a.checksum)\n"
    )
    calls: list[str] = []
    original = Engine.compile

    def counting(self: Engine, *args: Any, **kwargs: Any) -> Any:
        calls.append("compile")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Engine, "compile", counting)
    cli.ok("apply", cfg, "--state", tmp_path / "state.db", "-y")

    assert calls == ["compile"]
    assert (tmp_path / "b.txt").exists()


def test_run_async_sizes_the_default_executor_for_parallelism() -> None:
    """``width`` blocking calls must all be in flight at once to pass the barrier."""
    width = io_workers() + 8
    barrier = threading.Barrier(width, timeout=5)

    async def all_at_once() -> Result[int, AtlantideError]:
        await asyncio.gather(*(asyncio.to_thread(barrier.wait) for _ in range(width)))
        return Success(width)

    assert run_async(all_at_once(), parallelism=width).unwrap() == width


def test_io_workers_never_shrinks_below_asyncios_default() -> None:
    assert io_workers(1) == min(32, (os.cpu_count() or 1) + 4)
    assert io_workers(64) == 64
