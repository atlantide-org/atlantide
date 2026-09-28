"""CLI core fixes: raised errors render, --json stays parseable, the root callback
stays out of the way, Ctrl-C keeps working while threads drain, progress is locked."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import signal
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from returns.result import Failure, Result, Success

from atlantide.cli.errors import run_async
from atlantide.cli.progress import ProgressTable
from atlantide.cli.target import StateTarget
from atlantide.core import AtlantideError
from atlantide.core.actions import Action
from atlantide.core.errors import LockError, SecretsError, StateError
from atlantide.providers import loader
from atlantide.reconcile.progress import Phase
from tests.cli.conftest import file_config
from tests.support import Cli

cli = Cli()

# The package re-exports the commands under the module names, shadowing them.
migrate_mod = importlib.import_module("atlantide.cli.commands.state.migrate")
snapshot_mod = importlib.import_module("atlantide.cli.commands.state.snapshot")


# -- 1. errors raised rather than returned ------------------------------------


def test_unopenable_state_is_a_json_envelope_not_a_traceback(tmp_path: Path) -> None:
    cfg = file_config(tmp_path)
    result = cli.fails("plan", cfg, "--state", tmp_path / "missing" / "x.db", "--json")
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["kind"] == "StateError"


def test_unopenable_state_is_a_diagnostic_in_human_mode(tmp_path: Path) -> None:
    cfg = file_config(tmp_path)
    result = cli.fails("plan", cfg, "--state", tmp_path / "missing" / "x.db")
    assert "error:" in result.output
    assert "Traceback" not in result.output
    assert not isinstance(result.exception, AtlantideError)


def test_a_lock_error_from_backup_is_rendered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    @contextmanager
    def held(*_: Any, **__: Any) -> Any:
        raise LockError("state is locked by someone else")
        yield  # pragma: no cover

    monkeypatch.setattr(snapshot_mod, "held_lock", held)
    result = cli.fails("state", "backup", tmp_path / "b", "--state", tmp_path / "s.db")
    assert "state is locked by someone else" in result.output


def test_engine_for_does_not_leak_the_backend_when_secrets_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[_Backend] = []

    def fake_open(self: StateTarget) -> _Backend:
        backend = _Backend()
        opened.append(backend)
        return backend

    def broken_secrets(self: StateTarget) -> Any:
        raise SecretsError("keyfile unreadable")

    monkeypatch.setattr(StateTarget, "open", fake_open)
    monkeypatch.setattr(StateTarget, "secrets", broken_secrets)
    result = cli.fails("plan", file_config(tmp_path), "--state", tmp_path / "s.db")
    assert "keyfile unreadable" in result.output
    assert all(b.closed for b in opened)


@dataclass
class _Backend:
    closed: bool = False

    def close(self) -> None:
        self.closed = True


@dataclass
class _Remote:
    backend: _Backend | None = None
    label: str = "remote"

    def open(self) -> _Backend:
        if self.backend is None:
            raise StateError("remote unreachable")
        return self.backend


def test_adopt_remote_closes_the_local_side_when_the_remote_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = _Backend()
    monkeypatch.setattr(migrate_mod, "SqliteStateBackend", lambda _: local)
    source = tmp_path / "s.db"
    source.write_bytes(b"")
    with pytest.raises(StateError):
        migrate_mod._adopt_remote(_Remote(), source)  # type: ignore[arg-type]
    assert local.closed


def test_adopt_local_closes_the_remote_side_when_the_local_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = _Backend()

    def broken(_: str) -> Any:
        raise StateError("cannot open local")

    monkeypatch.setattr(migrate_mod, "SqliteStateBackend", broken)
    with pytest.raises(StateError):
        migrate_mod._adopt_local(_Remote(remote), tmp_path / "out.db")  # type: ignore[arg-type]
    assert remote.closed


# -- 2. plugin warning under --json -------------------------------------------


@dataclass
class _BrokenEntryPoint:
    name: str = "broken"

    def load(self) -> Any:
        raise ImportError("no module named boom")


def test_a_broken_plugin_warning_stays_off_json_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(loader, "entry_points", lambda group: [_BrokenEntryPoint()])
    result = cli.ok("plan", file_config(tmp_path), "--state", tmp_path / "s.db", "--json")
    assert json.loads(result.stdout)["ok"] is True
    assert "was not loaded" in result.stderr


# -- 3. the root callback -----------------------------------------------------


def test_a_broken_toml_does_not_block_help(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "atlantide.toml").write_text("this is [ not toml\n")
    assert "Usage" in cli.ok("plan", "--help").output


def test_a_broken_toml_under_json_is_an_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "atlantide.toml").write_text("this is [ not toml\n")
    result = cli.fails("plan", "--json")
    assert json.loads(result.stdout)["ok"] is False


def test_an_unlocked_component_blocks_plan_but_not_state_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "atlantide.toml").write_text("")
    (tmp_path / ".atlantis" / "components" / "rogue").mkdir(parents=True)
    cli.ok("state", "list", "--state", tmp_path / "s.db")
    cli.ok("secret", "list", "--state", tmp_path / "s.db")
    planned = cli.fails("plan", file_config(tmp_path), "--state", tmp_path / "s.db")
    assert "no entry in" in planned.output


# -- 4. Ctrl-C while executor threads drain -----------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX signal delivery")
def test_a_second_interrupt_while_threads_drain_still_abandons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the first Ctrl-C the task is cancelled at once, but asyncio.run still
    waits on the boto thread; the second Ctrl-C must still exit, not raise."""
    exited: list[int] = []
    monkeypatch.setattr(os, "_exit", lambda code: exited.append(code))

    def blocking_call() -> None:
        time.sleep(0.3)
        os.kill(os.getpid(), signal.SIGINT)  # the second press, mid-drain
        time.sleep(0.3)

    async def run() -> Result[str, AtlantideError]:
        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.to_thread(blocking_call)
        return Success("unreachable")  # pragma: no cover

    before = signal.getsignal(signal.SIGINT)
    result = run_async(run())

    assert isinstance(result, Failure)
    assert exited == [130]
    assert signal.getsignal(signal.SIGINT) is before


# -- 5. progress table locking ------------------------------------------------


def test_progress_table_records_and_renders_concurrently() -> None:
    nodes = [(f"local.Null:n{i}", Action.CREATE) for i in range(100)]
    table = ProgressTable(nodes)
    stop = threading.Event()
    errors: list[BaseException] = []

    def render() -> None:
        while not stop.is_set():
            try:
                table.__rich__()
            except BaseException as exc:  # pragma: no cover - the regression
                errors.append(exc)
                return

    renderer = threading.Thread(target=render)
    renderer.start()
    try:
        for _ in range(20):
            for node_id, action in nodes:
                table.record(node_id, action, Phase.START)
                table.record(node_id, action, Phase.FINISH)
            for i in range(50):
                table.record(f"local.Null:extra{_}-{i}", Action.CREATE, Phase.START)
    finally:
        stop.set()
        renderer.join()
    assert not errors
    assert hasattr(table, "_lock")
