"""Two runs at once: what the lease excludes, and what it must reload.

The lock is taken after the plan is computed. These cover that window: a
changeset describing state that has since moved, and an owner identity too coarse
to distinguish two concurrent runs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from atlantide.core import is_successful
from atlantide.engine import Engine
from atlantide.engine.locking import lock_owner
from atlantide.providers import local
from atlantide.providers.local import LocalProvider
from atlantide.state import MemoryStateBackend
from tests.conftest import make_engine


def _config(tmp: Path) -> str:
    return (
        "from atlantide.providers.local import File\n"
        f"a = File('a', path={str(tmp / 'a.txt')!r}, content='alpha')\n"
        f"File('b', path={str(tmp / 'b.txt')!r}, content=a.checksum)\n"
    )


def test_lock_owner_is_unique_per_acquisition() -> None:
    """Host+pid is not an identity for a run.

    `Lease.blocks` returns False for the same owner and `release_lock` drops every
    row an owner holds, so two engines in one process would neither exclude each
    other nor keep their own locks.
    """
    assert lock_owner() != lock_owner()


async def test_two_engines_in_one_process_exclude_each_other() -> None:
    backend = MemoryStateBackend()
    held = backend.acquire_lock(lock_owner(), 300, {"default:local.File:a"})
    assert is_successful(held)

    engine = make_engine(local.TYPES, LocalProvider(), backend=backend)
    result = await engine.apply(_config(Path("/tmp/atlantide-test-unused")))
    assert not is_successful(result)
    assert "locked" in str(result.failure())


async def test_releasing_one_run_does_not_free_another_runs_locks() -> None:
    backend = MemoryStateBackend()
    first, second = lock_owner(), lock_owner()
    assert is_successful(backend.acquire_lock(first, 300, {"n1"}))
    backend.release_lock(second)  # a *different* run finishing

    assert not is_successful(backend.acquire_lock("third-party", 300, {"n1"}))


async def test_apply_re_diffs_against_state_read_under_the_lease(tmp_path: Path) -> None:
    """A node created by another run between plan and lock must not be created twice.

    Executing the pre-lock changeset acts on `CREATE` for something that already
    exists, leaving the first resource live with no state row.
    """
    backend = MemoryStateBackend()
    engine: Engine = make_engine(local.TYPES, LocalProvider(), backend=backend)
    cfg = _config(tmp_path)

    # Another run gets there first and completes.
    other: Engine = make_engine(local.TYPES, LocalProvider(), backend=backend)
    assert is_successful(await other.apply(cfg))

    report = (await engine.apply(cfg)).unwrap()
    assert report.created == [], "state read under the lease already has both nodes"
    assert sorted(report.noop) == ["default:local.File:a", "default:local.File:b"]


async def test_concurrent_applies_of_one_config_serialise(tmp_path: Path) -> None:
    """Both runs want the same nodes, so exactly one holds the lease at a time."""
    backend = MemoryStateBackend()
    cfg = _config(tmp_path)
    engines = [make_engine(local.TYPES, LocalProvider(), backend=backend) for _ in range(2)]

    results = await asyncio.gather(*(e.apply(cfg) for e in engines))
    created = [len(r.unwrap().created) for r in results if is_successful(r)]
    # Either the second was refused the lease, or it ran after and saw both nodes.
    assert sum(created) == 2, f"a resource was created twice: {created}"
