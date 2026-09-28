"""Adding or removing ``prevent_destroy`` through the CLI.

A protect-only edit needs no provider call, but it is still a change: ``plan``
shows it, ``--detailed-exitcode`` counts it, and ``apply`` persists it instead of
reporting "nothing to apply".
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import Cli, write_config

cli = Cli()


def _config(tmp_path: Path, *, protect: bool, path: str = "p.txt") -> Path:
    lifecycle = ", lifecycle=Lifecycle(prevent_destroy=True)" if protect else ""
    return write_config(
        tmp_path,
        "from atlantide.core import Lifecycle\n"
        "from atlantide.providers.local import File\n"
        f"File('p', path={str(tmp_path / path)!r}, content='x'{lifecycle})\n",
    )


def test_plan_shows_a_protect_only_change_and_counts_it(tmp_path: Path) -> None:
    state = tmp_path / "state.db"
    cli.ok("apply", _config(tmp_path, protect=False), "--state", state, "-y")
    cfg = _config(tmp_path, protect=True)

    shown = cli.ok("plan", cfg, "--state", state)
    assert "prevent_destroy: false → true, state only" in shown.output

    assert cli.run("plan", cfg, "--state", state, "--detailed-exitcode").exit_code == 2

    data = json.loads(cli.ok("plan", cfg, "--state", state, "--json").output)
    (change,) = data["changes"]
    assert change["action"] == "noop"
    assert change["state_only"] is True
    assert change["lifecycle_changes"] == {"prevent_destroy": [False, True]}


def test_apply_persists_the_flag_then_destroy_is_refused(tmp_path: Path) -> None:
    state = tmp_path / "state.db"
    cli.ok("apply", _config(tmp_path, protect=False), "--state", state, "-y")
    cfg = _config(tmp_path, protect=True)

    applied = cli.ok("apply", cfg, "--state", state, "-y", "--json")
    assert json.loads(applied.output)["state_only"] == ["default:local.File:p"]
    assert cli.run("plan", cfg, "--state", state, "--detailed-exitcode").exit_code == 0

    refused = cli.run("destroy", "--state", state, "-y")
    assert refused.exit_code == 1
    assert "prevent_destroy" in refused.output
    assert (tmp_path / "p.txt").exists()

    # Remove it again: the same state-only write unlocks destroy.
    cli.ok("apply", _config(tmp_path, protect=False), "--state", state, "-y")
    cli.ok("destroy", "--state", state, "-y")
    assert not (tmp_path / "p.txt").exists()


def test_protect_added_with_a_replace_refuses_the_plan(tmp_path: Path) -> None:
    state = tmp_path / "state.db"
    cli.ok("apply", _config(tmp_path, protect=False), "--state", state, "-y")
    moved = _config(tmp_path, protect=True, path="q.txt")  # path is immutable

    result = cli.run("plan", moved, "--state", state)
    assert result.exit_code == 1
    assert "prevent_destroy" in result.output
