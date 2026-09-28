"""The confirmation prompt: asked on a terminal, refused without one, skipped by ``-y``."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.cli.conftest import file_config
from tests.support import Cli

cli = Cli()


@pytest.fixture
def interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the confirmation prompt believe it has a terminal.

    `CliRunner` supplies stdin as a plain stream, which is not a tty — so without
    this the TTY guard fires and the prompt never runs. Tests that exercise the
    *prompt* have to opt into looking interactive; tests that exercise the guard
    must not use this.
    """
    monkeypatch.setattr("atlantide.cli.options.stdin_is_tty", lambda: True)


def test_destroy_previews_before_prompt(tmp_path: Path, interactive: None) -> None:
    cfg = file_config(tmp_path)
    state = tmp_path / "state.db"
    cli.ok("apply", cfg, "--state", state, "-y")
    # answer "n": preview shown, prompt asked, nothing destroyed
    result = cli.run("destroy", "--state", state, input="n\n")
    assert result.exit_code != 0  # aborted
    assert "- destroy" in result.output and "local.File:f" in result.output
    assert "Destroy these 1 resource(s)?" in result.output


def test_apply_prompts_and_aborts_on_no(tmp_path: Path, interactive: None) -> None:
    cfg = file_config(tmp_path)
    state = tmp_path / "state.db"
    out = tmp_path / "out.txt"
    # answer "n" to the confirmation prompt
    result = cli.run("apply", cfg, "--state", state, input="n\n")
    assert result.exit_code != 0  # typer aborts
    assert "Apply these changes?" in result.output
    assert not out.exists()  # nothing applied


def test_apply_prompts_and_proceeds_on_yes(tmp_path: Path, interactive: None) -> None:
    cfg = file_config(tmp_path)
    state = tmp_path / "state.db"
    out = tmp_path / "out.txt"
    cli.run("apply", cfg, "--state", state, input="y\n")
    assert out.read_text() == "hi"


def test_a_prompt_with_no_terminal_names_the_flag_to_use(tmp_path: Path) -> None:
    """A command moved into CI has no terminal to prompt on.

    `typer.confirm` against a closed stdin aborts with "EOF when reading a line",
    which names the mechanism and not the fix.
    """
    cfg = file_config(tmp_path)
    state = tmp_path / "state.db"

    result = cli.run("apply", cfg, "--state", state)

    assert result.exit_code == 1
    assert "not a terminal" in result.output
    assert "--confirm" in result.output
    assert not (tmp_path / "out.txt").exists(), "nothing was applied"


def test_confirm_still_bypasses_the_prompt_entirely(tmp_path: Path) -> None:
    """The guard must not fire when the operator already said yes."""
    cfg = file_config(tmp_path)
    state = tmp_path / "state.db"
    cli.run("apply", cfg, "--state", state, "-y")


def test_destroy_without_a_terminal_is_refused(tmp_path: Path) -> None:
    """Destroy is where prompting into a pipe matters most."""
    cfg = file_config(tmp_path)
    state = tmp_path / "state.db"
    cli.ok("apply", cfg, "--state", state, "-y")

    result = cli.run("destroy", "--state", state)
    assert result.exit_code == 1
    assert "not a terminal" in result.output
    assert (tmp_path / "out.txt").exists(), "nothing was destroyed"
