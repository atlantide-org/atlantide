"""``--json`` means stdout is one parseable document, whatever the outcome.

The failure this guards against is narrow and nasty: a CI job whose parser works
until the day something goes wrong, because success emitted JSON and failure
emitted Rich-formatted text. A consumer cannot branch on that — it has to parse
before it knows — so the failing case is exactly the one it must be able to read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import Cli

cli = Cli()


# `mix_stderr=False` is what makes the assertions meaningful: the banners and
# warnings this suite is about must not be on stdout, so stdout has to be
# captured separately from stderr.


def _config(tmp_path: Path, *, broken: bool = False) -> Path:
    cfg = tmp_path / "config.py"
    if broken:
        cfg.write_text("import os\n")  # a non-allowlisted import
    else:
        cfg.write_text(
            "from atlantide.providers.local import File\n"
            f"File('f', path={str(tmp_path / 'out.txt')!r}, content='hi')\n"
        )
    return cfg


def _failing_config(tmp_path: Path) -> Path:
    """A config whose provider call fails: the target's parent is a regular file."""
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    cfg = tmp_path / "config.py"
    cfg.write_text(
        "from atlantide.providers.local import File\n"
        f"File('f', path={str(blocker / 'child.txt')!r}, content='hi')\n"
    )
    return cfg


def _sole_document(output: str) -> dict:
    """Parse stdout, asserting it is exactly one JSON document and nothing else."""
    return json.loads(output)


# -- success ------------------------------------------------------------------


def test_plan_json_is_one_document(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    result = cli.run("plan", cfg, "--state", tmp_path / "s.db", "--json")
    payload = _sole_document(result.stdout)
    assert payload["ok"] is True
    assert payload["schema_version"] == 1


def test_apply_json_is_one_document(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    result = cli.run("apply", cfg, "--state", tmp_path / "s.db", "--json", "-y")
    assert _sole_document(result.stdout)["ok"] is True


def test_refresh_json_is_one_document(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    state = tmp_path / "s.db"
    cli.ok("apply", cfg, "--state", state, "-y")
    result = cli.run("refresh", "--state", state, "--json")
    assert _sole_document(result.stdout)["ok"] is True


# -- failure ------------------------------------------------------------------


def test_a_config_error_is_still_json(tmp_path: Path) -> None:
    """The compile-time failure path, which renders a source snippet and caret in
    text mode — none of which is parseable."""
    cfg = _config(tmp_path, broken=True)
    result = cli.run("plan", cfg, "--state", tmp_path / "s.db", "--json")

    assert result.exit_code == 1
    payload = _sole_document(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["kind"] == "LanguageError"
    assert "os" in payload["error"]["message"]


def test_a_provider_failure_is_still_json_and_names_the_node(tmp_path: Path) -> None:
    cfg = _failing_config(tmp_path)
    result = cli.run("apply", cfg, "--state", tmp_path / "s.db", "--json", "-y")

    assert result.exit_code == 1
    error = _sole_document(result.stdout)["error"]
    assert error["kind"] == "ProviderError"
    assert error["node_id"] == "default:local.File:f"
    assert error["op"] == "create"


def test_a_plain_diagnostic_is_still_json(tmp_path: Path) -> None:
    """`fail()` messages bypass the error taxonomy; they still have to be
    parseable rather than left as the one un-JSON path."""
    result = cli.run("plan", tmp_path / "missing.py", "--state", tmp_path / "s.db", "--json")
    assert result.exit_code == 1
    assert _sole_document(result.stdout)["ok"] is False


# -- stream separation --------------------------------------------------------


def test_the_state_banner_does_not_land_on_stdout(tmp_path: Path) -> None:
    """Printed before every state-touching command. On stdout it would sit in
    front of the payload and break the parse on *success*."""
    cfg = _config(tmp_path)
    result = cli.run("plan", cfg, "--state", tmp_path / "s.db", "--json")
    assert "state:" not in result.stdout
    _sole_document(result.stdout)  # parses


def test_the_state_override_warning_does_not_land_on_stdout(tmp_path: Path) -> None:
    """`--state` against a configured remote backend warns loudly — and used to
    warn onto stdout, corrupting the document of a command that then succeeded."""
    (tmp_path / "atlantide.toml").write_text(
        '[state]\nbackend = "s3"\nbucket = "b"\nkey = "k"\nlock_table = "t"\n'
    )
    cfg = _config(tmp_path)
    monkey_cwd = tmp_path
    result = cli.run(
        "plan",
        cfg,
        "--state",
        tmp_path / "s.db",
        "--json",
        catch_exceptions=False,
        env={"PWD": str(monkey_cwd)},
    )
    # Whatever the outcome, stdout must be parseable on its own.
    _sole_document(result.stdout)


def test_state_list_json_is_one_document(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    state = tmp_path / "s.db"
    cli.ok("apply", cfg, "--state", state, "-y")
    result = cli.run("state", "list", "--state", state, "--json")
    assert "state:" not in result.stdout
    assert _sole_document(result.stdout)["nodes"]


def test_text_mode_still_prints_everything_to_stdout(tmp_path: Path) -> None:
    """The split is for JSON mode only; a human running the command should not
    have to merge two streams to read it."""
    cfg = _config(tmp_path)
    result = cli.run("plan", cfg, "--state", tmp_path / "s.db")
    assert "state:" in result.stdout
    assert "create" in result.stdout


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
