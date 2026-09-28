"""The ``secret`` group against the local keyfile value store."""

from __future__ import annotations

from pathlib import Path

from tests.support import Cli

cli = Cli()


def test_secret_set_get_list_rm_roundtrip(tmp_path: Path) -> None:
    state = str(tmp_path / "s.db")

    cli.run("secret", "set", "app/key", "hunter2", "--state", state)

    listed = cli.ok("secret", "list", "--state", state)
    assert "app/key" in listed.output
    assert "hunter2" not in listed.output  # list never shows values

    # get requires --reveal
    guarded = cli.run("secret", "get", "app/key", "--state", state)
    assert guarded.exit_code == 1
    assert "hunter2" not in guarded.output

    revealed = cli.run("secret", "get", "app/key", "-r", "--state", state)
    assert revealed.output.strip() == "hunter2"

    assert cli.run("secret", "rm", "app/key", "--state", state).exit_code == 0
    missing = cli.run("secret", "get", "app/key", "-r", "--state", state)
    assert missing.exit_code == 1  # gone -> error, no traceback
