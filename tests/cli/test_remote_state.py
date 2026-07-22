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
from typer.testing import CliRunner

from atlantide.cli.main import app
from atlantide.state.codec import StateDocument, loads
from atlantide.state.s3_backend import S3StateBackend
from tests.support import TEST_REGION, create_state_store, fake_aws_credentials

runner = CliRunner()

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
    first = runner.invoke(app, ["apply", "config.py", "--confirm"])
    assert first.exit_code == 0, first.output
    assert (project / "out.txt").read_text() == "hi"
    assert not (project / "atlantide.db").exists()  # nothing landed locally
    assert NODE_ID in _remote_state().nodes

    second = runner.invoke(app, ["plan", "config.py"])
    assert second.exit_code == 0, second.output
    assert "Plan: 1 unchanged" in second.output


def test_destroy_clears_the_remote_state(project: Path) -> None:
    runner.invoke(app, ["apply", "config.py", "--confirm"])
    destroy = runner.invoke(app, ["destroy", "--confirm"])
    assert destroy.exit_code == 0, destroy.output
    assert _remote_state().nodes == {}


def test_state_flag_overrides_the_remote_backend_loudly(project: Path) -> None:
    result = runner.invoke(
        app, ["apply", "config.py", "--state", "local.db", "--confirm"]
    )
    assert result.exit_code == 0, result.output
    assert "overrides" in result.output
    assert (project / "local.db").exists()


def test_migrate_copies_local_state_to_the_remote_backend(project: Path) -> None:
    applied = runner.invoke(
        app, ["apply", "config.py", "--state", "local.db", "--confirm"]
    )
    assert applied.exit_code == 0, applied.output

    migrated = runner.invoke(app, ["state", "migrate", "--from", "local.db", "--confirm"])
    assert migrated.exit_code == 0, migrated.output
    assert NODE_ID in _remote_state().nodes

    # With state now remote, the config is already applied: nothing to do.
    plan = runner.invoke(app, ["plan", "config.py"])
    assert "Plan: 1 unchanged" in plan.output


def test_migrate_refuses_to_overwrite_populated_remote_state(project: Path) -> None:
    runner.invoke(app, ["apply", "config.py", "--confirm"])  # remote now has a node
    runner.invoke(app, ["apply", "config.py", "--state", "local.db", "--confirm"])

    result = runner.invoke(app, ["state", "migrate", "--from", "local.db", "--confirm"])
    assert result.exit_code != 0
    assert "already holds 1 node(s)" in result.output


def test_migrate_needs_a_remote_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["state", "migrate", "--confirm"])
    assert result.exit_code != 0
    assert "no remote backend configured" in result.output


def test_migrate_reports_a_missing_source(project: Path) -> None:
    result = runner.invoke(app, ["state", "migrate", "--from", "absent.db", "--confirm"])
    assert result.exit_code != 0
    assert "no local state database" in result.output


def test_commands_announce_which_state_they_target(project: Path) -> None:
    """Pointing at the wrong shared state is silent unless the command says so."""
    result = runner.invoke(app, ["plan", "config.py"])
    assert f"s3://{BUCKET}/{KEY}" in result.output


def test_json_output_carries_the_state_target_instead_of_the_banner(project: Path) -> None:
    result = runner.invoke(app, ["plan", "config.py", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["state"] == f"s3://{BUCKET}/{KEY}"


def test_the_project_file_is_found_from_a_subdirectory(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the parent walk this would silently plan against a fresh local database."""
    nested = project / "stacks"
    nested.mkdir()
    monkeypatch.chdir(nested)
    result = runner.invoke(app, ["plan", str(project / "config.py")])
    assert result.exit_code == 0, result.output
    assert f"s3://{BUCKET}/{KEY}" in result.output


def test_state_check_reports_the_bucket_and_lock_table(project: Path) -> None:
    result = runner.invoke(app, ["state", "check"])
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
        result = runner.invoke(app, ["state", "check", "--no-probe"])
    assert result.exit_code == 1
    assert "fail" in result.output


def test_state_unlock_lists_holds_without_breaking_them(project: Path) -> None:
    backend = _live_backend()
    backend.acquire_lock("ci-runner-7", 300, {NODE_ID})

    result = runner.invoke(app, ["state", "unlock"])
    assert result.exit_code == 0, result.output
    assert "ci-runner-7" in result.output
    assert set(_live_backend().locks()) == {NODE_ID}  # still held


def test_state_unlock_breaks_a_dead_runs_hold(project: Path) -> None:
    _live_backend().acquire_lock("ci-runner-7", 300, {NODE_ID})

    result = runner.invoke(app, ["state", "unlock", "--owner", "ci-runner-7", "--confirm"])
    assert result.exit_code == 0, result.output
    assert "unlocked 1" in result.output
    assert _live_backend().locks() == {}


def test_state_unlock_rejects_an_unknown_owner(project: Path) -> None:
    _live_backend().acquire_lock("ci-runner-7", 300, {NODE_ID})
    result = runner.invoke(app, ["state", "unlock", "--owner", "nobody", "--confirm"])
    assert result.exit_code != 0
    assert "no locks held by 'nobody'" in result.output


def test_migrate_back_to_a_local_database(project: Path) -> None:
    runner.invoke(app, ["apply", "config.py", "--confirm"])

    result = runner.invoke(
        app, ["state", "migrate", "--to-local", "local.db", "--confirm"]
    )
    assert result.exit_code == 0, result.output
    assert (project / "local.db").exists()

    plan = runner.invoke(app, ["plan", "config.py", "--state", "local.db"])
    assert "Plan: 1 unchanged" in plan.output


def test_migrate_can_be_forced_over_populated_state(project: Path) -> None:
    runner.invoke(app, ["apply", "config.py", "--confirm"])
    runner.invoke(app, ["apply", "config.py", "--state", "local.db", "--confirm"])

    result = runner.invoke(
        app, ["state", "migrate", "--from", "local.db", "--force", "--confirm"]
    )
    assert result.exit_code == 0, result.output
    assert NODE_ID in _remote_state().nodes


def test_migrate_says_the_local_database_is_now_stale(project: Path) -> None:
    runner.invoke(app, ["apply", "config.py", "--state", "local.db", "--confirm"])
    result = runner.invoke(app, ["state", "migrate", "--from", "local.db", "--confirm"])
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
        default = runner.invoke(app, ["plan"])
        overlay = runner.invoke(app, ["--profile", "other", "plan"])
    assert f"s3://{BUCKET}/{KEY}" in default.output
    assert f"s3://{BUCKET}/other/atlantide.json" in overlay.output


def _live_backend() -> S3StateBackend:
    """A backend pointed at the same mocked store the CLI is using."""
    return S3StateBackend(BUCKET, KEY, lock_table=LOCK_TABLE, region=REGION)
