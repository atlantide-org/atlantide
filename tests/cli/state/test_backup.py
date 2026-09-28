"""``atlantide state backup`` / ``restore``: snapshot the store and put one back.

These run against the local sqlite backend, which keeps no history of its own:
a bad write there is unrecoverable without a snapshot.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from atlantide.state import SqliteStateBackend
from atlantide.state.codec import decode
from tests.cli.state.conftest import apply_project, write_project
from tests.support import Cli

cli = Cli()


def test_backup_then_restore_round_trips_nodes_and_outputs(tmp_path: Path) -> None:
    _, state = apply_project(tmp_path, with_output=True)
    snapshot = tmp_path / "snap.atlas-state"

    result = cli.ok("state", "backup", snapshot, "--state", state)
    assert "backed up 1 node(s)" in result.output

    before = SqliteStateBackend(str(state))
    original_nodes, original_outputs = before.load().nodes, before.outputs()
    before.close()
    assert original_outputs  # the declared output() was committed

    # Lose state the way a bad write would: forget the node and the outputs.
    scratch = SqliteStateBackend(str(state))
    for node_id in list(original_nodes):
        scratch.delete(node_id)
    scratch.set_outputs({}, remove=list(original_outputs))
    scratch.close()

    cli.ok("state", "restore", snapshot, "--state", state, "--force", "-y")

    after = SqliteStateBackend(str(state))
    assert after.load().nodes == original_nodes
    assert after.outputs() == original_outputs
    after.close()


def test_restore_refuses_a_snapshot_state_has_moved_past(tmp_path: Path) -> None:
    """The serial guard is what makes restore safe.

    A snapshot older than current state describes a different set of live
    resources; restoring it silently would strand everything created since.
    """
    cfg, state = apply_project(tmp_path, with_output=True)
    snapshot = tmp_path / "snap.atlas-state"
    cli.ok("state", "backup", snapshot, "--state", state)

    # Move state on, so its serial no longer matches the snapshot's.
    cfg.write_text(
        "from atlantide.providers.local import File\n"
        f"File('f', path={str(tmp_path / 'out.txt')!r}, content='changed')\n"
    )
    assert cli.run("apply", cfg, "--state", state, "-y").exit_code == 0

    refused = cli.run("state", "restore", snapshot, "--state", state, "-y")
    assert refused.exit_code == 1
    assert "written to since" in refused.output

    cli.run("state", "restore", snapshot, "--state", state, "--force", "-y")


def test_restore_names_the_nodes_it_will_stop_tracking(tmp_path: Path) -> None:
    """A node in state but not in the snapshot is forgotten, not destroyed:
    the resource outlives the state row, so the preview has to say so."""
    _, state = apply_project(tmp_path, with_output=True)
    snapshot = tmp_path / "snap.atlas-state"
    cli.ok("state", "backup", snapshot, "--state", state)

    # A second resource that the snapshot does not know about.
    cfg2 = tmp_path / "two.py"
    cfg2.write_text(
        "from atlantide.providers.local import File\n"
        f"File('f', path={str(tmp_path / 'out.txt')!r}, content='hi')\n"
        f"File('g', path={str(tmp_path / 'other.txt')!r}, content='yo')\n"
    )
    assert cli.run("apply", cfg2, "--state", state, "-y").exit_code == 0

    result = cli.ok("state", "restore", snapshot, "--state", state, "--force", "-y")
    assert "forgotten" in result.output
    assert "local.File:g" in result.output
    assert "destroyed" in result.output  # single token: Rich wraps at word boundaries
    # ...and it is gone from state, while the snapshot's node stays.
    backend = SqliteStateBackend(str(state))
    ids = set(backend.load().nodes)
    backend.close()
    assert not any(nid.endswith(":g") for nid in ids)
    assert any(nid.endswith(":f") for nid in ids)


def test_backup_will_not_silently_overwrite_a_snapshot(tmp_path: Path) -> None:
    _, state = apply_project(tmp_path, with_output=True)
    snapshot = tmp_path / "snap.atlas-state"
    cli.ok("state", "backup", snapshot, "--state", state)

    again = cli.run("state", "backup", snapshot, "--state", state)
    assert again.exit_code == 1
    assert "already exists" in again.output

    cli.run("state", "backup", snapshot, "--state", state, "--force")


def test_backup_defaults_to_a_name_carrying_the_serial(tmp_path: Path, monkeypatch) -> None:
    _, state = apply_project(tmp_path, with_output=True)
    monkeypatch.chdir(tmp_path)

    cli.ok("state", "backup", "--state", state)
    written = list(tmp_path.glob("atlantide-state-*.atlas-state"))
    assert len(written) == 1
    doc = decode(written[0].read_bytes())
    assert f"atlantide-state-{doc.serial}-" in written[0].name


def test_restore_reports_an_unreadable_snapshot_as_a_diagnostic(tmp_path: Path) -> None:
    _, state = apply_project(tmp_path, with_output=True)

    missing = cli.run("state", "restore", tmp_path / "nope", "--state", state)
    assert missing.exit_code == 1
    assert "no snapshot at" in missing.output

    junk = tmp_path / "junk.atlas-state"
    junk.write_bytes(b"not a state document")
    corrupt = cli.run("state", "restore", junk, "--state", state, "-y")
    assert corrupt.exit_code == 1
    assert "cannot read snapshot" in corrupt.output
    assert "Traceback" not in corrupt.output


def test_backup_of_empty_state_is_a_valid_snapshot(tmp_path: Path) -> None:
    """An empty scope is a documented no-op lock, so this exercises the path
    where `held_lock` acquires nothing."""
    _, state = write_project(tmp_path, with_output=True)
    snapshot = tmp_path / "empty.atlas-state"
    cli.run("state", "backup", snapshot, "--state", state)
    assert decode(snapshot.read_bytes()).nodes == {}


def test_backup_refuses_a_node_created_while_waiting_for_the_lock(
    tmp_path: Path, monkeypatch
) -> None:
    """The lock scope is computed before the lease is taken, so a node id born
    in between is not covered by it; an apply writing that node could tear the
    snapshot. The re-check after acquiring has to catch it, as destroy() does."""
    from contextlib import contextmanager

    from atlantide.cli.commands.state import snapshot as snapshot_cmd
    from tests.support import state_node

    _, state = apply_project(tmp_path, with_output=True)
    real_held_lock = snapshot_cmd.held_lock

    @contextmanager
    def racing_held_lock(backend, scope, **kw):
        # A concurrent apply lands a new node just before the lease is acquired.
        backend.put(state_node("sneaky", type="local.File", provider="local"))
        with real_held_lock(backend, scope, **kw):
            yield

    monkeypatch.setattr(snapshot_cmd, "held_lock", racing_held_lock)
    refused = cli.run("state", "backup", tmp_path / "torn.atlas-state", "--state", state)
    assert refused.exit_code == 1
    assert "gained node(s)" in refused.output
    assert "sneaky" in refused.output
    assert not (tmp_path / "torn.atlas-state").exists()
    # ...and the failure released the lease it took.
    backend = SqliteStateBackend(str(state))
    held = backend.locks()
    backend.close()
    assert held == {}


def test_backup_releases_the_lock_it_took(tmp_path: Path) -> None:
    """A snapshot that left its lease behind would block the next apply until
    the TTL lapsed, turning a read-only command into an outage."""
    _, state = apply_project(tmp_path, with_output=True)
    snapshot = tmp_path / "snap.atlas-state"
    cli.ok("state", "backup", snapshot, "--state", state)
    backend = SqliteStateBackend(str(state))
    held = backend.locks()
    backend.close()
    assert held == {}


def test_backup_is_owner_only(tmp_path: Path) -> None:
    _, state = apply_project(tmp_path, with_output=True)
    snapshot = tmp_path / "snap.atlas-state"
    cli.ok("state", "backup", snapshot, "--state", state)
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
    cli.ok("state", "backup", snapshot, "--state", state, "--force")
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600


def test_backup_does_not_write_through_a_planted_symlink(tmp_path: Path) -> None:
    _, state = apply_project(tmp_path, with_output=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me\n")
    link = tmp_path / "snap.atlas-state"
    link.symlink_to(victim)

    refused = cli.run("state", "backup", link, "--state", state)
    assert refused.exit_code == 1
    assert "already exists" in refused.output
    assert victim.read_text() == "keep me\n"

    # --force replaces the link itself; the file it pointed at is untouched.
    cli.ok("state", "backup", link, "--state", state, "--force")
    assert not link.is_symlink()
    assert decode(link.read_bytes()).nodes
    assert victim.read_text() == "keep me\n"
    assert not list(tmp_path.glob(".snap.atlas-state.*.tmp"))


def test_restore_refuses_a_write_that_lands_while_it_waits_for_the_lock(
    tmp_path: Path, monkeypatch
) -> None:
    """The serial check and the diff are only a preview until the lease is held;
    a write landing in between must abort rather than be overwritten."""
    from contextlib import contextmanager

    from atlantide.cli.commands.state import snapshot as snapshot_cmd
    from tests.support import state_node

    _, state = apply_project(tmp_path, with_output=True)
    snapshot = tmp_path / "snap.atlas-state"
    cli.ok("state", "backup", snapshot, "--state", state)
    real_held_lock = snapshot_cmd.held_lock

    @contextmanager
    def racing_held_lock(backend, scope, **kw):
        backend.put(state_node("sneaky", type="local.File", provider="local"))
        with real_held_lock(backend, scope, **kw):
            yield

    monkeypatch.setattr(snapshot_cmd, "held_lock", racing_held_lock)
    refused = cli.run("state", "restore", snapshot, "--state", state, "-y")
    assert refused.exit_code == 1
    assert "while restore waited for the lock" in refused.output
    backend = SqliteStateBackend(str(state))
    try:
        assert "default:local.File:sneaky" in backend.load().nodes, refused.output
        assert backend.locks() == {}
    finally:
        backend.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
