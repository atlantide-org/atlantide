"""Regressions: `apply`/`refresh --json`, `output`, `deploy`, policy text, `state rm`."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from atlantide.cli.console import console
from atlantide.cli.views.plan import render_violations
from atlantide.core import PolicyLevel
from atlantide.policy.base import Violation
from tests.cli.conftest import file_config
from tests.support import Cli, write_config

cli = Cli()


@pytest.fixture
def interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the confirmation prompt believe it has a terminal."""
    monkeypatch.setattr("atlantide.cli.options.stdin_is_tty", lambda: True)


def _denied_config(tmp_path: Path) -> Path:
    """A plan a mandatory policy denies (as in ``test_run_commands``)."""
    cfg = tmp_path / "config.py"
    cfg.write_text(
        "from atlantide.core import Stack\n"
        "from atlantide.policy import enforce\n"
        "from atlantide.providers.aws import S3Bucket\n"
        "enforce('require-tags')\n"
        "with Stack('dev', region='us-east-1'):\n"
        "    S3Bucket('b', bucket='no-tags-bucket')\n"
    )
    return cfg


# -- item 1: --json always prints a document ------------------------------------


def test_apply_json_dry_run_emits_the_plan(tmp_path: Path) -> None:
    cfg = file_config(tmp_path)
    result = cli.ok("apply", cfg, "--state", tmp_path / "s.db", "--json", "--dry-run")
    doc = json.loads(result.stdout)
    assert doc["ok"] is True and doc["schema_version"] == 1
    assert doc["dry_run"] is True and doc["applied"] is False
    assert [c["action"] for c in doc["changes"]] == ["create"]
    assert "state" in doc
    assert not (tmp_path / "out.txt").exists()


def test_apply_json_with_nothing_to_change_emits_the_plan(tmp_path: Path) -> None:
    cfg = file_config(tmp_path)
    state = tmp_path / "s.db"
    first = json.loads(cli.ok("apply", cfg, "--state", state, "--json", "-y").stdout)
    assert first["applied"] is True
    doc = json.loads(cli.ok("apply", cfg, "--state", state, "--json", "-y").stdout)
    assert doc["applied"] is False and doc["dry_run"] is False
    assert doc["summary"]["create"] == 0


def test_refresh_json_on_empty_state_emits_an_empty_drift_document(tmp_path: Path) -> None:
    result = cli.ok("refresh", "--state", tmp_path / "s.db", "--json")
    doc = json.loads(result.stdout)
    assert doc["ok"] is True
    assert doc["drift"] is False and doc["nodes"] == []
    assert "state" in doc


# -- item 2: --json never prompts -----------------------------------------------


def test_apply_json_without_confirm_fails_instead_of_prompting(
    tmp_path: Path, interactive: None
) -> None:
    cfg = file_config(tmp_path)
    result = cli.run("apply", cfg, "--state", tmp_path / "s.db", "--json", input="y\n")
    assert result.exit_code == 1
    doc = json.loads(result.stdout)  # nothing but the error envelope on stdout
    assert doc["ok"] is False
    assert "--confirm" in doc["error"]["message"]
    assert not (tmp_path / "out.txt").exists()


# -- item 3: a denied plan fails apply as it fails plan -------------------------


def test_apply_dry_run_on_a_denied_plan_exits_1(tmp_path: Path) -> None:
    result = cli.run("apply", _denied_config(tmp_path), "--state", tmp_path / "s.db", "--dry-run")
    assert result.exit_code == 1
    assert "DENY" in result.output


def test_apply_on_a_denied_plan_does_not_prompt(tmp_path: Path, interactive: None) -> None:
    result = cli.run("apply", _denied_config(tmp_path), "--state", tmp_path / "s.db", input="y\n")
    assert result.exit_code == 1
    assert "Apply these changes?" not in result.output


def test_apply_json_on_a_denied_plan_emits_the_blocked_plan(tmp_path: Path) -> None:
    cfg = _denied_config(tmp_path)
    result = cli.run("apply", cfg, "--state", tmp_path / "s.db", "--json", "-y")
    assert result.exit_code == 1
    doc = json.loads(result.stdout)
    assert doc["blocked"] is True and doc["applied"] is False


# -- item 4: flag validation honours --json -------------------------------------


def test_apply_json_bad_on_failure_is_a_json_error(tmp_path: Path) -> None:
    cfg = file_config(tmp_path)
    result = cli.run("apply", cfg, "--state", tmp_path / "s.db", "--json", "--on-failure", "bogus")
    assert result.exit_code == 1
    doc = json.loads(result.stdout)
    assert doc["ok"] is False and "--on-failure" in doc["error"]["message"]


# -- item 5: output decrypts only what it prints --------------------------------


def _sensitive_applied(tmp_path: Path) -> Path:
    cfg = write_config(
        tmp_path,
        "from atlantide.core import output\n"
        "from atlantide.providers.random import Password\n"
        "p = Password('p', length=12)\n"
        "output('secret_value', p.result)\n"
        "output('plain', 'visible')\n",
    )
    state = tmp_path / "state.db"
    cli.ok("apply", cfg, "--state", state, "-y")
    return state


def test_output_without_reveal_needs_no_keyfile(tmp_path: Path) -> None:
    state = _sensitive_applied(tmp_path)
    keyfile = tmp_path / "atlantide.key"
    assert keyfile.exists()
    keyfile.unlink()

    listed = cli.ok("output", "--state", state)
    assert "(sensitive)" in listed.output and "visible" in listed.output
    assert cli.ok("output", "plain", "--state", state).stdout.strip() == "visible"
    listed_json = json.loads(cli.ok("output", "--state", state, "--json").stdout)
    assert listed_json["outputs"]["default:plain"] == "visible"
    assert not keyfile.exists(), "no replacement keyfile was written"


def test_output_reveal_still_unseals(tmp_path: Path) -> None:
    state = _sensitive_applied(tmp_path)
    assert len(cli.ok("output", "secret_value", "--state", state, "-r").stdout.strip()) == 12
    listed = cli.ok("output", "--state", state, "-r").output
    assert "(sensitive)" not in listed


# -- items 6 and 7: deploy ------------------------------------------------------


def test_deploy_announces_the_target_before_prompting(tmp_path: Path, interactive: None) -> None:
    art = tmp_path / "app.atlas"
    cli.ok("build", file_config(tmp_path), "-o", art)
    result = cli.run("deploy", art, "--state", tmp_path / "s.db", input="n\n")
    assert result.exit_code != 0
    assert "state:" in result.output
    assert result.output.index("state:") < result.output.index("Deploy ")


def test_deploy_sizes_the_executor_from_parallelism(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from atlantide.cli.commands import artifact as artifact_cmd

    seen: dict[str, Any] = {}
    real = artifact_cmd.run_async

    def spy(coro: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return real(coro, **kwargs)

    monkeypatch.setattr(artifact_cmd, "run_async", spy)
    art = tmp_path / "app.atlas"
    cli.ok("build", file_config(tmp_path), "-o", art)
    cli.ok("deploy", art, "--state", tmp_path / "s.db", "-y", "--parallelism", "3")
    assert seen.get("parallelism") == 3


# -- item 8: policy text is not markup ------------------------------------------


def test_render_violations_escapes_markup() -> None:
    violation = Violation(
        policy="p[/x]", level=PolicyLevel.MANDATORY, node_id="n", message="bad [/y] tag"
    )
    plan_obj = SimpleNamespace(violations=[violation], blocked=(violation,))
    with console.capture() as captured:
        render_violations(plan_obj)  # type: ignore[arg-type]
    assert "p[/x]: bad [/y] tag" in captured.get()


# -- item 9: state rm forgets in one write --------------------------------------


def test_state_rm_forgets_every_node_in_one_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from atlantide.state.sql.sqlite import SqliteStateBackend

    cfg = write_config(
        tmp_path,
        "from atlantide.providers.local import File\n"
        f"File('a', path={str(tmp_path / 'a.txt')!r}, content='a')\n"
        f"File('b', path={str(tmp_path / 'b.txt')!r}, content='b')\n",
    )
    state = tmp_path / "state.db"
    cli.ok("apply", cfg, "--state", state, "-y")

    def no_per_node_delete(self: Any, node_id: str) -> None:
        raise AssertionError(f"rm deleted {node_id} on its own")

    monkeypatch.setattr(SqliteStateBackend, "delete", no_per_node_delete)
    monkeypatch.chdir(tmp_path)  # the backup lands in the working directory
    cli.ok("state", "rm", "default:local.File:a", "default:local.File:b", "--state", state, "-y")
    monkeypatch.undo()
    assert "local.File" not in cli.ok("state", "list", "--state", state, "--json").stdout
