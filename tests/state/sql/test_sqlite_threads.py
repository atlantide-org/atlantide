"""The sqlite backend driven from the apply's writer thread.

It may be offloaded (``offload_writes``; off by default only for speed), so its
connection is opened with ``check_same_thread=False`` and may be used from the
``atlantide-state`` thread while the loop thread keeps calling it directly
(committed outputs, compensations, the pre-run load, lock administration). A
sqlite3 connection is not safe for simultaneous use, so the backend serializes
every call itself; these tests verify that.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from returns.pipeline import is_successful

from atlantide.state import SqliteStateBackend

from ..conftest import node

OWNER = "run-1"
WRITES = 400


def test_usable_from_a_thread_other_than_its_creator(tmp_path: Path) -> None:
    backend = SqliteStateBackend(str(tmp_path / "s.db"))
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="atlantide-state") as pool:
            pool.submit(backend.put, node("a")).result()
            pool.submit(backend.set_outputs, {"s:k": "v"}).result()
            assert pool.submit(backend.serial).result() == 1
        # And back on the creating thread.
        assert set(backend.load().nodes) == {"a"}
        assert backend.outputs() == {"s:k": "v"}
    finally:
        backend.close()


def test_loop_thread_reads_alongside_writer_thread_writes(tmp_path: Path) -> None:
    """The apply's shape: fenced puts on the writer, everything else on the loop.

    Without the backend's own serialization this fails within a few hundred
    calls with "recursive use of cursors", a commit landing inside the other
    thread's ``BEGIN IMMEDIATE``, or "cannot start a transaction within a
    transaction".
    """
    backend = SqliteStateBackend(str(tmp_path / "s.db"))
    scope = {f"n{i}" for i in range(WRITES)}
    lease = backend.acquire_lock(OWNER, 60.0, scope)
    assert is_successful(lease)
    backend.bind_lease(lease.unwrap())
    errors: list[BaseException] = []
    done = threading.Event()

    def write_all() -> None:
        try:
            for i in range(WRITES):
                backend.put(node(f"n{i}"))
                if i % 50 == 0:
                    backend.put_many([node(f"n{i}", input_hash="h1")])
        finally:
            done.set()

    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="atlantide-state") as pool:
            written = pool.submit(write_all)
            rounds = 0
            while not done.is_set() or rounds < 20:
                try:
                    backend.load()
                    backend.serial()
                    backend.outputs()
                    backend.locks()
                    backend.set_outputs({f"s:r{rounds % 5}": rounds})
                except BaseException as exc:  # recorded, so the writer is still joined
                    errors.append(exc)
                    break
                rounds += 1
            written.result()
    finally:
        backend.close()

    assert errors == []
    reopened = SqliteStateBackend(str(tmp_path / "s.db"))
    try:
        assert set(reopened.load().nodes) == scope
        assert set(reopened.outputs()) == {f"s:r{n}" for n in range(5)}
    finally:
        reopened.close()
