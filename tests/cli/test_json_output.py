"""``--json`` means stdout is one parseable document, whatever the outcome.

Guards against success emitting JSON while failure emits Rich-formatted text. A
consumer has to parse before it knows the outcome, so the failing case must be
parseable too.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.cli.conftest import file_config
from tests.support import Cli, write_config

cli = Cli()


# The assertions read `result.stdout`, captured separately from stderr: the
# banners and warnings this suite covers must not be on stdout.


def _config(tmp_path: Path, *, broken: bool = False) -> Path:
    if broken:
        return write_config(tmp_path, "import os\n")  # a non-allowlisted import
    return file_config(tmp_path)


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
    text mode, none of which is parseable."""
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
    """`fail()` messages bypass the error taxonomy; they must still be parseable."""
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
    """`--state` against a configured remote backend warns; on stdout the warning
    would corrupt the document of a command that then succeeds."""
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


def test_emit_json_matches_rich_print_json_byte_for_byte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``emit_json`` writes plain JSON without Rich, yet must produce exactly the
    bytes ``print_json`` does on a non-terminal (same indent, key coercion,
    ``default=str`` fallback, non-ASCII kept) and never ANSI, even when stdout is
    a terminal."""
    import io

    from rich.console import Console

    from atlantide.cli.views import output

    payload = {
        "tuple": (1, 2.5, None),
        1: "int key",
        "unicode": "café → ✓",
        "path": Path("/x/y"),
        "nested": {"b": [True, {"c": "d"}], "a": {}},
    }
    expected = io.StringIO()
    Console(file=expected, force_terminal=False, width=40).print_json(
        json.dumps({"schema_version": output.SCHEMA_VERSION, "ok": True, **payload}, default=str)
    )
    actual = io.StringIO()
    monkeypatch.setattr(output, "console", Console(file=actual, force_terminal=True, width=40))
    output.emit_json(payload)  # type: ignore[arg-type]
    assert actual.getvalue() == expected.getvalue()
    assert "\x1b" not in actual.getvalue()
