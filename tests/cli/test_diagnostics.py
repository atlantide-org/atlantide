"""Diagnostics: what a failing command says, and that it is never a bare traceback."""

from __future__ import annotations

from pathlib import Path

from tests.support import Cli

cli = Cli()


def _failing_config(tmp_path: Path) -> tuple[Path, str]:
    """A config whose File write fails: its parent path is a regular file."""
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    target = blocker / "child.txt"
    cfg = tmp_path / "config.py"
    cfg.write_text(
        "from atlantide.providers.local import File\n"
        f"File('f', path={str(target)!r}, content='hi')\n"
    )
    return cfg, "default:local.File:f"


def test_apply_failure_names_the_node_and_op(tmp_path: Path) -> None:
    cfg, node = _failing_config(tmp_path)
    state = tmp_path / "state.db"
    result = cli.run("apply", cfg, "--state", state, "-y")
    assert result.exit_code == 1
    # the failing resource + op are surfaced, not just a bare provider message
    assert node in result.output
    assert "op=create" in result.output


def test_debug_flag_adds_a_traceback(tmp_path: Path) -> None:
    cfg, _ = _failing_config(tmp_path)
    state = tmp_path / "state.db"
    plain = cli.run("apply", cfg, "--state", state, "-y")
    debug = cli.run("--debug", "apply", cfg, "--state", state, "-y")
    assert debug.exit_code == 1
    assert "Traceback" in debug.output
    assert "Traceback" not in plain.output  # off by default


def test_debug_can_be_switched_on_from_the_environment(tmp_path: Path) -> None:
    """A CI job re-run with ``ATLANTIDE_DEBUG=1`` gets the traceback without
    editing the command line it runs."""
    cfg, _ = _failing_config(tmp_path)
    state = tmp_path / "state.db"
    debug = cli.run("apply", cfg, "--state", state, "-y", env={"ATLANTIDE_DEBUG": "1"})
    assert debug.exit_code == 1
    assert "Traceback" in debug.output
    quiet = cli.run("apply", cfg, "--state", state, "-y", env={"ATLANTIDE_DEBUG": "0"})
    assert "Traceback" not in quiet.output


def test_plan_on_invalid_config_errors(tmp_path: Path) -> None:
    cfg = tmp_path / "bad.py"
    cfg.write_text("import os\n")  # non-allowlisted import
    result = cli.run("plan", cfg, "--state", tmp_path / "s.db")
    assert result.exit_code == 1
    assert "error" in result.output


def test_diagnostic_shows_source_snippet_and_caret(tmp_path: Path) -> None:
    cfg = tmp_path / "bad.py"
    cfg.write_text("x = 1\nwhile True:\n    pass\n")
    result = cli.run("plan", cfg, "--state", tmp_path / "s.db")
    assert result.exit_code == 1
    assert "while True:" in result.output  # the offending source line
    assert "^" in result.output  # the caret
    assert "(line 2" in result.output  # the position


def test_plan_without_config_or_project_errors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = cli.run("plan")
    assert result.exit_code == 1
    assert "no config given" in result.output


def test_a_missing_config_path_gets_a_diagnostic_not_a_traceback(tmp_path: Path) -> None:
    """A mistyped config path gets a diagnostic instead of the Python traceback
    an unguarded `read_text` would raise."""
    result = cli.run("plan", tmp_path / "nope.py", "--state", tmp_path / "s.db")
    assert result.exit_code == 1
    assert "cannot read config" in result.output
    assert "Traceback" not in result.output
