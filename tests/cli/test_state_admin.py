"""``state list`` / ``show`` / ``rm``: what state contains, not where it lives.

Without these, state is a black box — you can verify the backend and move it
wholesale, but you cannot see one row or repair one row. The interesting cases
are the ones an operator reaches for when something has already gone wrong.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from atlantide.state import SqliteStateBackend
from atlantide.state.backend import NO_INPUT_HASH
from tests.support import Cli

cli = Cli()

NODE = "default:local.File:f"


def _project(tmp_path: Path, *, protected: bool = False) -> tuple[Path, Path]:
    cfg = tmp_path / "config.py"
    lifecycle = ", lifecycle=Lifecycle(prevent_destroy=True)" if protected else ""
    cfg.write_text(
        "from atlantide.core import Lifecycle\n"
        "from atlantide.providers.local import File\n"
        f"File('f', path={str(tmp_path / 'out.txt')!r}, content='hi'{lifecycle})\n"
    )
    return cfg, tmp_path / "state.db"


def _applied(tmp_path: Path, **kw: bool) -> tuple[Path, Path]:
    cfg, state = _project(tmp_path, **kw)
    cli.run("apply", cfg, "--state", state, "-y")
    return cfg, state


# -- list ---------------------------------------------------------------------


def test_list_shows_what_state_records(tmp_path: Path) -> None:
    _, state = _applied(tmp_path)
    result = cli.run("state", "list", "--state", state)
    assert "local.File:f" in result.output
    assert "created" in result.output


def test_list_of_empty_state_says_so_rather_than_printing_nothing(tmp_path: Path) -> None:
    _, state = _project(tmp_path)
    result = cli.run("state", "list", "--state", state)
    assert "state is empty" in result.output


def test_list_marks_a_row_the_next_plan_cannot_skip(tmp_path: Path) -> None:
    """`NO_INPUT_HASH` is how a row that stopped describing reality becomes
    visible — set by `refresh --write` on drift, and by a failed rollback. It is
    invisible in every other view, so `list` is where it has to surface."""
    _, state = _applied(tmp_path)
    backend = SqliteStateBackend(str(state))
    node = backend.load().nodes[NODE]
    backend.put(replace(node, input_hash=NO_INPUT_HASH))
    backend.close()

    result = cli.run("state", "list", "--state", state)
    assert "DRIFTED" in result.output


def test_list_json_carries_the_same_facts(tmp_path: Path) -> None:
    _, state = _applied(tmp_path)
    result = cli.run("state", "list", "--state", state, "--json")
    payload = json.loads(result.output)
    assert payload["nodes"][0]["node_id"] == NODE
    assert payload["nodes"][0]["drifted"] is False


# -- show ---------------------------------------------------------------------


def test_show_prints_the_inputs_and_outputs_of_one_node(tmp_path: Path) -> None:
    _, state = _applied(tmp_path)
    result = cli.run("state", "show", NODE, "--state", state)
    assert "local.File" in result.output
    assert "Inputs:" in result.output
    assert "Outputs:" in result.output
    assert "checksum" in result.output


def test_show_of_an_unknown_node_points_at_list(tmp_path: Path) -> None:
    _, state = _applied(tmp_path)
    result = cli.run("state", "show", "nope", "--state", state)
    assert result.exit_code == 1
    assert "no node" in result.output
    assert "state list" in result.output


def test_show_json_is_a_single_document(tmp_path: Path) -> None:
    """The state banner would otherwise land on stdout and corrupt the payload."""
    _, state = _applied(tmp_path)
    result = cli.run("state", "show", NODE, "--state", state, "--json")
    payload = json.loads(result.output)
    assert payload["node_id"] == NODE
    assert payload["properties"]["content"] == "hi"


# -- rm -----------------------------------------------------------------------


def test_rm_forgets_a_node_without_destroying_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The distinction the command exists to make. `destroy` removes the
    resource; `rm` removes only atlantide's record of it."""
    _, state = _applied(tmp_path)
    monkeypatch.chdir(tmp_path)  # `rm` snapshots into the cwd
    out = tmp_path / "out.txt"
    assert out.exists()

    cli.run("state", "rm", NODE, "--state", state, "-y")
    assert out.exists(), "the file itself is untouched"
    backend = SqliteStateBackend(str(state))
    assert NODE not in backend.load().nodes
    backend.close()


def test_rm_says_plainly_that_the_resource_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Someone reaching for this may think it is a destroy. The warning is the
    difference between forgetting a stale row and duplicating live infra."""
    _, state = _applied(tmp_path)
    monkeypatch.chdir(tmp_path)  # `rm` snapshots into the cwd
    result = cli.run("state", "rm", NODE, "--state", state, "-y")
    assert "not destroyed" in result.output
    assert "create them again" in result.output


def test_rm_refuses_an_unknown_node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo must not read as "forgot what you asked for"."""
    _, state = _applied(tmp_path)
    monkeypatch.chdir(tmp_path)  # `rm` snapshots into the cwd
    result = cli.run("state", "rm", "nope", "--state", state, "-y")
    assert result.exit_code == 1
    assert "not in state" in result.output


def test_rm_refuses_a_protected_node_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, state = _applied(tmp_path, protected=True)
    monkeypatch.chdir(tmp_path)  # `rm` snapshots into the cwd

    refused = cli.run("state", "rm", NODE, "--state", state, "-y")
    assert refused.exit_code == 1
    assert "prevent_destroy" in refused.output

    cli.run("state", "rm", NODE, "--state", state, "--force", "-y")


def test_rm_snapshots_state_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Forgetting a row is unrecoverable without one, and the operator reaching
    for `rm` is already having a bad day."""
    _, state = _applied(tmp_path)
    monkeypatch.chdir(tmp_path)  # `rm` snapshots into the cwd

    cli.run("state", "rm", NODE, "--state", state, "-y")
    assert list(tmp_path.glob("atlantide-state-*.atlas-state")), "no backup was written"


def test_rm_can_skip_the_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, state = _applied(tmp_path)
    monkeypatch.chdir(tmp_path)  # `rm` snapshots into the cwd

    cli.run("state", "rm", NODE, "--state", state, "--no-backup", "-y")
    assert not list(tmp_path.glob("atlantide-state-*.atlas-state"))


def test_rm_releases_its_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, state = _applied(tmp_path)
    monkeypatch.chdir(tmp_path)  # `rm` snapshots into the cwd
    cli.ok("state", "rm", NODE, "--state", state, "--no-backup", "-y")
    backend = SqliteStateBackend(str(state))
    assert backend.locks() == {}
    backend.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
