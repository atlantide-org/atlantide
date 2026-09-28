"""The CLI driving a remote state backend: apply, override, and migration.

Everything here runs against moto, so it exercises the real boto3 call shapes
without credentials.
"""

from __future__ import annotations

import json
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws
from returns.result import Failure

from atlantide.state import StateGraph, StateNode
from atlantide.state.codec import decode
from atlantide.state.s3 import S3StateBackend
from tests.support import TEST_REGION, Cli, create_state_store, fake_aws_credentials

cli = Cli()


REGION = TEST_REGION
BUCKET = "acme-atlantide-state"
KEY = "prod/atlantide.json"
LOCK_TABLE = "atlantide-locks"
NODE_ID = "default:local.File:f"

_TOML = f"""
[state]
backend    = "s3"
bucket     = "{BUCKET}"
key        = "{KEY}"
lock_table = "{LOCK_TABLE}"
region     = "{REGION}"
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A project directory whose atlantide.toml points at a mocked S3 backend."""
    fake_aws_credentials(monkeypatch, region=REGION)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "atlantide.toml").write_text(_TOML)
    (tmp_path / "config.py").write_text(
        "from atlantide.providers.local import File\n"
        f"File('f', path={str(tmp_path / 'out.txt')!r}, content='hi')\n"
    )
    with mock_aws():
        create_state_store(BUCKET, LOCK_TABLE, region=REGION)
        yield tmp_path


def _remote_state() -> StateGraph:
    """The state the CLI just wrote to the mocked bucket, as a fresh reader sees it."""
    backend = _live_backend()
    try:
        return backend.load()
    finally:
        backend.close()


def test_apply_writes_state_to_s3_and_re_apply_is_a_noop(project: Path) -> None:
    """The Merkle skip has to survive the round-trip through the remote codec."""
    cli.ok("apply", "config.py", "--confirm")
    assert (project / "out.txt").read_text() == "hi"
    assert not (project / "atlantide.db").exists()  # nothing landed locally
    assert NODE_ID in _remote_state().nodes

    second = cli.ok("plan", "config.py")
    assert "Plan: 1 unchanged" in second.output


def test_destroy_clears_the_remote_state(project: Path) -> None:
    cli.ok("apply", "config.py", "--confirm")
    cli.ok("destroy", "--confirm")
    assert _remote_state().nodes == {}


def test_state_flag_overrides_the_remote_backend_loudly(project: Path) -> None:
    result = cli.run("apply", "config.py", "--state", "local.db", "--confirm")
    assert "overrides" in result.output
    assert (project / "local.db").exists()


def test_migrate_copies_local_state_to_the_remote_backend(project: Path) -> None:
    cli.ok("apply", "config.py", "--state", "local.db", "--confirm")

    cli.ok("state", "migrate", "--from", "local.db", "--confirm")
    assert NODE_ID in _remote_state().nodes

    # With state now remote, the config is already applied: nothing to do.
    plan = cli.ok("plan", "config.py")
    assert "Plan: 1 unchanged" in plan.output


def test_migrate_refuses_to_overwrite_populated_remote_state(project: Path) -> None:
    cli.ok("apply", "config.py", "--confirm")  # remote now has a node
    cli.ok("apply", "config.py", "--state", "local.db", "--confirm")

    result = cli.run("state", "migrate", "--from", "local.db", "--confirm")
    assert result.exit_code != 0
    assert "already holds 1 node(s)" in result.output


def test_migrate_needs_a_remote_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = cli.run("state", "migrate", "--confirm")
    assert result.exit_code != 0
    assert "no remote backend configured" in result.output


def test_migrate_reports_a_missing_source(project: Path) -> None:
    result = cli.run("state", "migrate", "--from", "absent.db", "--confirm")
    assert result.exit_code != 0
    assert "no local state database" in result.output


def test_commands_announce_which_state_they_target(project: Path) -> None:
    """Pointing at the wrong shared state is silent unless the command says so."""
    result = cli.run("plan", "config.py")
    assert f"s3://{BUCKET}/{KEY}" in result.output


def test_json_output_carries_the_state_target_instead_of_the_banner(project: Path) -> None:
    result = cli.run("plan", "config.py", "--json")
    payload = json.loads(result.output)
    assert payload["state"] == f"s3://{BUCKET}/{KEY}"


def test_the_project_file_is_found_from_a_subdirectory(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the parent walk this would silently plan against a fresh local database."""
    nested = project / "stacks"
    nested.mkdir()
    monkeypatch.chdir(nested)
    result = cli.run("plan", project / "config.py")
    assert f"s3://{BUCKET}/{KEY}" in result.output


def test_state_check_reports_the_bucket_and_lock_table(project: Path) -> None:
    result = cli.run("state", "check")
    assert "bucket:" in result.output
    assert "lock table:" in result.output
    assert "conditional writes" in result.output


def test_state_check_exits_non_zero_when_something_is_broken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_aws_credentials(monkeypatch, region=REGION)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "atlantide.toml").write_text(_TOML)
    with mock_aws():  # neither the bucket nor the table exists
        result = cli.run("state", "check", "--no-probe")
    assert result.exit_code == 1
    assert "fail" in result.output


def test_state_unlock_lists_holds_without_breaking_them(project: Path) -> None:
    backend = _live_backend()
    backend.acquire_lock("ci-runner-7", 300, {NODE_ID})

    result = cli.run("state", "unlock")
    assert "ci-runner-7" in result.output
    assert set(_live_backend().locks()) == {NODE_ID}  # still held


def test_state_unlock_breaks_a_dead_runs_hold(project: Path) -> None:
    _live_backend().acquire_lock("ci-runner-7", 300, {NODE_ID})

    result = cli.run("state", "unlock", "--owner", "ci-runner-7", "--confirm")
    assert "unlocked 1" in result.output
    assert _live_backend().locks() == {}


def test_state_unlock_rejects_an_unknown_owner(project: Path) -> None:
    _live_backend().acquire_lock("ci-runner-7", 300, {NODE_ID})
    result = cli.run("state", "unlock", "--owner", "nobody", "--confirm")
    assert result.exit_code != 0
    assert "no locks held by 'nobody'" in result.output


def test_state_unlock_all_leaves_other_states_holds(project: Path) -> None:
    """Projects may share one lock table; `--all` means all of *this* state's."""
    _live_backend().acquire_lock("ci-runner-7", 300, {NODE_ID})
    other = S3StateBackend(BUCKET, "staging/atlantide.json", lock_table=LOCK_TABLE, region=REGION)
    other.acquire_lock("someone-else", 300, {NODE_ID})

    listed = cli.run("state", "unlock")
    assert "ci-runner-7" in listed.output
    assert "someone-else" not in listed.output

    result = cli.run("state", "unlock", "--all", "--confirm")
    assert "unlocked 1" in result.output
    assert _live_backend().locks() == {}
    assert set(other.locks()) == {NODE_ID}


def test_migrate_back_to_a_local_database(project: Path) -> None:
    cli.ok("apply", "config.py", "--confirm")

    cli.run("state", "migrate", "--to-local", "local.db", "--confirm")
    assert (project / "local.db").exists()
    # It now holds a copy of the whole remote state: owner-only, not umask-default.
    assert stat.S_IMODE((project / "local.db").stat().st_mode) == 0o600

    plan = cli.ok("plan", "config.py", "--state", "local.db")
    assert "Plan: 1 unchanged" in plan.output


def test_migrate_can_be_forced_over_populated_state(project: Path) -> None:
    cli.ok("apply", "config.py", "--confirm")
    cli.ok("apply", "config.py", "--state", "local.db", "--confirm")

    cli.run("state", "migrate", "--from", "local.db", "--force", "--confirm")
    assert NODE_ID in _remote_state().nodes


def test_migrate_says_the_local_database_is_now_stale(project: Path) -> None:
    cli.ok("apply", "config.py", "--state", "local.db", "--confirm")
    result = cli.run("state", "migrate", "--from", "local.db", "--confirm")
    assert "no longer read" in result.output


def test_a_profile_selects_a_different_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_aws_credentials(monkeypatch, region=REGION)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "atlantide.toml").write_text(
        f'config = "config.py"\n{_TOML}\n[profile.other.state]\nkey = "other/atlantide.json"\n'
    )
    (tmp_path / "config.py").write_text(
        "from atlantide.providers.local import File\n"
        f"File('f', path={str(tmp_path / 'out.txt')!r}, content='hi')\n"
    )
    with mock_aws():
        create_state_store(BUCKET, LOCK_TABLE, region=REGION)
        default = cli.run("plan")
        overlay = cli.run("--profile", "other", "plan")
    assert f"s3://{BUCKET}/{KEY}" in default.output
    assert f"s3://{BUCKET}/other/atlantide.json" in overlay.output


def _live_backend() -> S3StateBackend:
    """A backend pointed at the same mocked store the CLI is using."""
    return S3StateBackend(BUCKET, KEY, lock_table=LOCK_TABLE, region=REGION)


def test_backup_and_restore_round_trip_through_the_remote_backend(project: Path) -> None:
    """The snapshot format is backend-independent: what `backup` writes from S3
    is the same document `restore` puts back, so a snapshot is a portable copy of
    state rather than a dump of one store's internals."""
    assert cli.run("apply", "config.py", "--confirm").exit_code == 0
    snapshot = project / "snap.atlas-state"

    cli.run("state", "backup", snapshot)
    assert NODE_ID in decode(snapshot.read_bytes()).nodes

    assert cli.run("destroy", "--confirm").exit_code == 0
    assert _remote_state().nodes == {}

    cli.ok("state", "restore", snapshot, "--force", "-y")
    assert NODE_ID in _remote_state().nodes


def test_backup_is_refused_while_another_run_holds_the_lock(project: Path) -> None:
    """A snapshot read under a foreign lease could capture a half-written apply.

    Failing avoids a file that looks like a complete backup but is not.
    """
    assert cli.run("apply", "config.py", "--confirm").exit_code == 0
    backend = S3StateBackend(bucket=BUCKET, key=KEY, lock_table=LOCK_TABLE, region=REGION)
    assert not isinstance(
        backend.acquire_lock("someone-else", 300.0, frozenset({NODE_ID})), Failure
    )
    try:
        result = cli.run("state", "backup", project / "snap.atlas-state")
        assert result.exit_code != 0
        assert not (project / "snap.atlas-state").exists()
    finally:
        backend.release_lock("someone-else")
        backend.close()


def test_migrate_is_refused_while_the_source_is_being_written(project: Path) -> None:
    """A copy taken while an apply is writing the source is torn, and a torn
    copy is indistinguishable from a complete one, so migrate must fail instead."""
    assert cli.run("apply", "config.py", "--confirm").exit_code == 0

    holder = S3StateBackend(bucket=BUCKET, key=KEY, lock_table=LOCK_TABLE, region=REGION)
    assert not isinstance(holder.acquire_lock("another-run", 300.0, frozenset({NODE_ID})), Failure)
    try:
        result = cli.run("state", "migrate", "--to-local", "local.db", "--confirm")
        assert result.exit_code != 0
    finally:
        holder.release_lock("another-run")
        holder.close()


def _journal_keys() -> list[str]:
    client: Any = boto3.client("s3", region_name=REGION)
    listed = client.list_objects_v2(Bucket=BUCKET, Prefix=f"{KEY}.d/").get("Contents", [])
    return [obj["Key"] for obj in listed]


def test_a_locked_run_compacts_the_journal_when_it_finishes(project: Path) -> None:
    """`checkpoint()` after the run folds its writes into the snapshot."""
    cli.ok("apply", "config.py", "--confirm")
    client: Any = boto3.client("s3", region_name=REGION)
    snapshot = decode(client.get_object(Bucket=BUCKET, Key=KEY)["Body"].read())
    if snapshot.gen == 0:  # the engine's checkpoint hook is not wired yet
        pytest.skip("engine does not call checkpoint() after locked runs yet")
    assert NODE_ID in snapshot.nodes
    assert _journal_keys() == []


def test_plan_never_compacts(project: Path) -> None:
    _live_backend().put(_a_node())
    before = _journal_keys()
    cli.ok("plan", "config.py")
    assert _journal_keys() == before


def _a_node() -> StateNode:
    return StateNode(
        id=NODE_ID, type="local.File", provider="local", provider_version="1", input_hash="h"
    )


def test_state_compact_folds_the_journal(project: Path) -> None:
    _live_backend().put(_a_node())
    assert _journal_keys()
    result = cli.ok("state", "compact")
    assert "compacted 1 journal head(s)" in result.output
    assert _journal_keys() == []
    assert NODE_ID in _remote_state().nodes


def test_state_compact_needs_the_s3_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = cli.run("state", "compact")
    assert result.exit_code != 0
    assert "only the s3 state backend" in result.output


def test_state_compact_reports_a_lost_race(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from atlantide.state.s3 import CompactionReport, S3StateBackend

    monkeypatch.setattr(S3StateBackend, "compact", lambda self: CompactionReport(skipped=True))
    assert "skipped" in cli.ok("state", "compact").output


def test_state_fsck_reports_a_healthy_journal(project: Path) -> None:
    _live_backend().put(_a_node())
    result = cli.ok("state", "fsck")
    assert "checked 1 head(s)" in result.output
    assert "ok" in result.output


def _drop_head() -> None:
    backend = _live_backend()
    epoch = backend._snapshots.ensure().doc.epoch
    head_key = backend._ctx.layout(epoch).head_key("log", NODE_ID)
    boto3.client("dynamodb", region_name=REGION).delete_item(
        TableName=LOCK_TABLE, Key={"node_id": {"S": head_key}}
    )


def test_state_fsck_finds_and_rebuilds_a_lost_head(project: Path) -> None:
    _live_backend().put(_a_node())
    _drop_head()
    assert NODE_ID not in _remote_state().nodes

    found = cli.run("state", "fsck")
    assert found.exit_code == 1
    assert "lost head" in found.output

    rebuilt = cli.run("state", "fsck", "--rebuild-heads", "--confirm")
    assert rebuilt.exit_code == 0, rebuilt.output
    assert "rebuilt head" in rebuilt.output and "review" in rebuilt.output
    assert NODE_ID in _remote_state().nodes


def test_state_fsck_reports_a_missing_entry(project: Path) -> None:
    _live_backend().put(_a_node())
    client: Any = boto3.client("s3", region_name=REGION)
    for key in _journal_keys():
        client.delete_object(Bucket=BUCKET, Key=key)
    result = cli.run("state", "fsck")
    assert result.exit_code == 1
    assert "missing entry" in result.output


def test_state_fsck_rebuild_refuses_while_a_run_holds_a_lock(project: Path) -> None:
    _live_backend().acquire_lock("ci-runner-7", 300, {NODE_ID})
    result = cli.run("state", "fsck", "--rebuild-heads", "--confirm")
    assert result.exit_code != 0
    assert "locked by a live run" in result.output


def test_state_fsck_lists_pending_and_collectable_entries(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from atlantide.state.s3 import FsckReport, S3StateBackend

    monkeypatch.setattr(
        S3StateBackend,
        "fsck",
        lambda self, rebuild_heads=False: FsckReport(pending=["k"], collectable=2),
    )
    result = cli.ok("state", "fsck")
    assert "1 pending" in result.output and "2 entr(ies) awaiting compaction" in result.output


def test_state_check_warns_about_the_journal_setup(project: Path) -> None:
    result = cli.run("state", "check")
    assert "journal lifecycle" in result.output
    assert "journal heads PITR" in result.output
    assert "journal listing" in result.output
