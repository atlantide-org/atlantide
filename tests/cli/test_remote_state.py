"""The CLI driving a remote state backend: apply, override, and migration.

Everything here runs against moto, so it exercises the real boto3 call shapes
without credentials.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws
from returns.result import Failure

from atlantide.state.codec import StateDocument, decode, loads
from atlantide.state.s3_backend import S3StateBackend
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


def _remote_state() -> StateDocument:
    """The state document the CLI just wrote to the mocked bucket."""
    client: Any = boto3.client("s3", region_name=REGION)
    return loads(client.get_object(Bucket=BUCKET, Key=KEY)["Body"].read())


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


def test_migrate_back_to_a_local_database(project: Path) -> None:
    cli.ok("apply", "config.py", "--confirm")

    cli.run("state", "migrate", "--to-local", "local.db", "--confirm")
    assert (project / "local.db").exists()

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

    Failing here is the point: the alternative is a file that looks like a
    complete backup and is not.
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
    """A copy taken while an apply is writing the source is torn — and a torn
    copy is indistinguishable from a complete one, so it has to fail loudly
    rather than produce a destination nobody can trust."""
    assert cli.run("apply", "config.py", "--confirm").exit_code == 0

    holder = S3StateBackend(bucket=BUCKET, key=KEY, lock_table=LOCK_TABLE, region=REGION)
    assert not isinstance(holder.acquire_lock("another-run", 300.0, frozenset({NODE_ID})), Failure)
    try:
        result = cli.run("state", "migrate", "--to-local", "local.db", "--confirm")
        assert result.exit_code != 0
    finally:
        holder.release_lock("another-run")
        holder.close()
