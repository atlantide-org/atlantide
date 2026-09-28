"""``state list`` / ``show`` / ``rm``: what state contains, not where it lives.

These inspect and repair individual rows, rather than verifying or moving the
backend wholesale. The cases covered are the ones an operator reaches for when
something has already gone wrong.
"""

from __future__ import annotations

import json
import stat
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from atlantide.state import NO_INPUT_HASH, SqliteStateBackend
from tests.cli.state.conftest import apply_project, write_project
from tests.support import Cli

cli = Cli()

NODE = "default:local.File:f"


# -- list ---------------------------------------------------------------------


def test_list_shows_what_state_records(tmp_path: Path) -> None:
    _, state = apply_project(tmp_path)
    result = cli.run("state", "list", "--state", state)
    assert "local.File:f" in result.output
    assert "created" in result.output


def test_list_of_empty_state_says_so_rather_than_printing_nothing(tmp_path: Path) -> None:
    _, state = write_project(tmp_path)
    result = cli.run("state", "list", "--state", state)
    assert "state is empty" in result.output


def test_list_marks_a_row_the_next_plan_cannot_skip(tmp_path: Path) -> None:
    """`NO_INPUT_HASH` is how a row that stopped describing reality becomes
    visible — set by `refresh --write` on drift, and by a failed rollback. It is
    invisible in every other view, so `list` is where it has to surface."""
    _, state = apply_project(tmp_path)
    backend = SqliteStateBackend(str(state))
    node = backend.load().nodes[NODE]
    backend.put(replace(node, input_hash=NO_INPUT_HASH))
    backend.close()

    result = cli.run("state", "list", "--state", state)
    assert "DRIFTED" in result.output


def test_list_json_carries_the_same_facts(tmp_path: Path) -> None:
    _, state = apply_project(tmp_path)
    result = cli.run("state", "list", "--state", state, "--json")
    payload = json.loads(result.output)
    assert payload["nodes"][0]["node_id"] == NODE
    assert payload["nodes"][0]["drifted"] is False


# -- show ---------------------------------------------------------------------


def test_show_prints_the_inputs_and_outputs_of_one_node(tmp_path: Path) -> None:
    _, state = apply_project(tmp_path)
    result = cli.run("state", "show", NODE, "--state", state)
    assert "local.File" in result.output
    assert "Inputs:" in result.output
    assert "Outputs:" in result.output
    assert "checksum" in result.output


def test_show_of_an_unknown_node_points_at_list(tmp_path: Path) -> None:
    _, state = apply_project(tmp_path)
    result = cli.run("state", "show", "nope", "--state", state)
    assert result.exit_code == 1
    assert "no node" in result.output
    assert "state list" in result.output


def test_show_json_is_a_single_document(tmp_path: Path) -> None:
    """The state banner would otherwise land on stdout and corrupt the payload."""
    _, state = apply_project(tmp_path)
    result = cli.run("state", "show", NODE, "--state", state, "--json")
    payload = json.loads(result.output)
    assert payload["node_id"] == NODE
    assert payload["properties"]["content"] == "hi"


# -- rm -----------------------------------------------------------------------


def test_rm_forgets_a_node_without_destroying_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`destroy` removes the resource; `rm` removes only atlantide's record of it."""
    _, state = apply_project(tmp_path)
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
    _, state = apply_project(tmp_path)
    monkeypatch.chdir(tmp_path)  # `rm` snapshots into the cwd
    result = cli.run("state", "rm", NODE, "--state", state, "-y")
    assert "not destroyed" in result.output
    assert "create them again" in result.output


def test_rm_refuses_an_unknown_node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo must not read as "forgot what you asked for"."""
    _, state = apply_project(tmp_path)
    monkeypatch.chdir(tmp_path)  # `rm` snapshots into the cwd
    result = cli.run("state", "rm", "nope", "--state", state, "-y")
    assert result.exit_code == 1
    assert "not in state" in result.output


def test_rm_refuses_a_protected_node_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, state = apply_project(tmp_path, protected=True)
    monkeypatch.chdir(tmp_path)  # `rm` snapshots into the cwd

    refused = cli.run("state", "rm", NODE, "--state", state, "-y")
    assert refused.exit_code == 1
    assert "prevent_destroy" in refused.output

    cli.run("state", "rm", NODE, "--state", state, "--force", "-y")


def test_rm_snapshots_state_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Forgetting a row is unrecoverable without a snapshot."""
    _, state = apply_project(tmp_path)
    monkeypatch.chdir(tmp_path)  # `rm` snapshots into the cwd

    cli.run("state", "rm", NODE, "--state", state, "-y")
    assert list(tmp_path.glob("atlantide-state-*.atlas-state")), "no backup was written"


def test_rm_snapshot_is_owner_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, state = apply_project(tmp_path)
    monkeypatch.chdir(tmp_path)  # `rm` snapshots into the cwd
    cli.ok("state", "rm", NODE, "--state", state, "-y")
    (snapshot,) = tmp_path.glob("atlantide-state-*.atlas-state")
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600


def test_precreated_local_database_is_owner_only(tmp_path: Path) -> None:
    """`migrate --to-local` pre-creates its destination this way before sqlite
    opens it; sqlite must accept the empty file and keep its mode."""
    from atlantide.util.fs import create_private

    fresh = tmp_path / "fresh.db"
    create_private(fresh, nofollow=True)
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o600
    SqliteStateBackend(str(fresh)).close()  # sqlite accepts the empty file
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o600

    existing = tmp_path / "existing.db"
    existing.write_bytes(b"")
    existing.chmod(0o640)
    create_private(existing, nofollow=True)  # an existing file's mode is left alone
    assert stat.S_IMODE(existing.stat().st_mode) == 0o640


def test_rm_can_skip_the_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, state = apply_project(tmp_path)
    monkeypatch.chdir(tmp_path)  # `rm` snapshots into the cwd

    cli.run("state", "rm", NODE, "--state", state, "--no-backup", "-y")
    assert not list(tmp_path.glob("atlantide-state-*.atlas-state"))


def test_rm_releases_its_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, state = apply_project(tmp_path)
    monkeypatch.chdir(tmp_path)  # `rm` snapshots into the cwd
    cli.ok("state", "rm", NODE, "--state", state, "--no-backup", "-y")
    backend = SqliteStateBackend(str(state))
    assert backend.locks() == {}
    backend.close()


# -- rm under the lock --------------------------------------------------------
#
# `rm` previews without the lock (so a wedged apply cannot block the
# prompt) and acts under it. The backup is part of the acting: taken before the
# lease, it could miss a write that landed while `rm` waited, and the operator
# would be holding a torn snapshot that reads as a complete one.

OTHER = "default:local.File:other"


def _seed_other(state: Path) -> None:
    """A second row, so a concurrent write has something besides NODE to touch."""
    from tests.support import state_node

    backend = SqliteStateBackend(str(state))
    try:
        backend.put(state_node("other", type="local.File", provider="local"))
    finally:
        backend.close()


def _racing_rm_lock(monkeypatch: pytest.MonkeyPatch, before_lock: Any) -> list[frozenset[str]]:
    """Run ``before_lock(backend)`` just before `rm` takes its lease; return the scopes."""
    from contextlib import contextmanager

    from atlantide.cli.commands.state import nodes as nodes_cmd
    from atlantide.engine.locking import held_lock as real_held_lock

    scopes: list[frozenset[str]] = []

    @contextmanager
    def racing_held_lock(backend: Any, scope: Any, **kw: Any) -> Iterator[Any]:
        scopes.append(frozenset(scope))
        before_lock(backend)
        with real_held_lock(backend, scope, **kw) as lease:
            yield lease

    monkeypatch.setattr(nodes_cmd, "held_lock", racing_held_lock)
    return scopes


def _state(state: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    backend = SqliteStateBackend(str(state))
    try:
        return dict(backend.load().nodes), backend.locks()
    finally:
        backend.close()


def test_rm_backup_includes_a_write_that_landed_while_it_waited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The snapshot is taken under the same lease as the delete, from state
    re-read under it, so it is the state the delete acted on."""
    from atlantide.state.codec import decode

    _, state = apply_project(tmp_path)
    _seed_other(state)
    monkeypatch.chdir(tmp_path)

    def concurrent_apply(backend: Any) -> None:
        node = backend.load().nodes[OTHER]
        backend.put(replace(node, properties={"content": "written meanwhile"}))

    scopes = _racing_rm_lock(monkeypatch, concurrent_apply)
    cli.ok("state", "rm", NODE, "--state", state, "-y")

    (snapshot,) = tmp_path.glob("atlantide-state-*.atlas-state")
    doc = decode(snapshot.read_bytes())
    assert doc.nodes[OTHER].properties == {"content": "written meanwhile"}
    assert NODE in doc.nodes, "the backup is taken before the delete"
    assert scopes == [frozenset({NODE, OTHER})], "a whole-state backup locks the whole state"
    nodes, locks = _state(state)
    assert NODE not in nodes
    assert locks == {}
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600


def test_rm_backup_refuses_a_node_created_while_it_waited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same rule `state backup` follows: a row born while waiting is outside
    the scope locked, so a writer could tear the snapshot around it."""
    from tests.support import state_node

    _, state = apply_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    _racing_rm_lock(
        monkeypatch,
        lambda backend: backend.put(state_node("sneaky", type="local.File", provider="local")),
    )

    refused = cli.fails("state", "rm", NODE, "--state", state, "-y")
    assert (
        "state gained node(s) while rm waited for the lock: default:local.File:sneaky"
        " — nothing was removed; re-run rm to review them"
    ) in refused.output
    assert not list(tmp_path.glob("atlantide-state-*.atlas-state")), "no backup was written"
    nodes, locks = _state(state)
    assert NODE in nodes, "nothing was removed"
    assert locks == {}


def test_rm_rechecks_the_nodes_it_removes_under_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The preview's checks are only a preview: a node deleted meanwhile is
    reported, not backed up and "forgotten" a second time."""
    _, state = apply_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    _racing_rm_lock(monkeypatch, lambda backend: backend.delete(NODE))

    refused = cli.fails("state", "rm", NODE, "--state", state, "-y")
    assert f"not in state: {NODE}" in refused.output
    assert not list(tmp_path.glob("atlantide-state-*.atlas-state"))
    assert _state(state)[1] == {}


def test_rm_rechecks_prevent_destroy_under_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, state = apply_project(tmp_path)
    monkeypatch.chdir(tmp_path)

    def protect(backend: Any) -> None:
        backend.put(replace(backend.load().nodes[NODE], prevent_destroy=True))

    _racing_rm_lock(monkeypatch, protect)
    refused = cli.fails("state", "rm", NODE, "--state", state, "-y")
    assert "prevent_destroy" in refused.output
    assert NODE in _state(state)[0]


def test_rm_without_backup_locks_only_what_it_removes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no snapshot to keep consistent, an unrelated row appearing is
    ignored, and a narrow lease leaves other applies alone."""
    from tests.support import state_node

    _, state = apply_project(tmp_path)
    _seed_other(state)
    monkeypatch.chdir(tmp_path)
    scopes = _racing_rm_lock(
        monkeypatch,
        lambda backend: backend.put(state_node("sneaky", type="local.File", provider="local")),
    )

    cli.ok("state", "rm", NODE, "--state", state, "--no-backup", "-y")
    assert scopes == [frozenset({NODE})]
    nodes, locks = _state(state)
    assert NODE not in nodes
    assert {OTHER, "default:local.File:sneaky"} <= set(nodes)
    assert locks == {}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
