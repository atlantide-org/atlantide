"""``atlantide import`` end to end, as a user runs it.

The reconcile suite proves the row; this proves the command — the argument
shapes, the exit codes a CI job branches on, and the listing that answers "what
can I even import here". The local provider is used throughout so none of it
needs credentials.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.support import Cli

cli = Cli()

CONFIG = """
from atlantide.providers.local import File

File('greeting', path={path!r}, content='hello\\n')
"""
NODE_ID = "default:local.File:greeting"


def _project(tmp_path: Path, *, exists: bool = True) -> tuple[Path, Path]:
    """A config declaring one file, and the state db to import into.

    ``exists`` writes the file first — the resource being adopted has to be
    already there, which for the local provider means on disk.
    """
    target = tmp_path / "greeting.txt"
    if exists:
        target.write_text("hello\n")
    cfg = tmp_path / "infra.py"
    cfg.write_text(CONFIG.format(path=str(target)))
    return cfg, tmp_path / "state.db"


def test_import_then_plan_reports_no_changes(tmp_path: Path) -> None:
    """The command-level version of the headline assertion."""
    cfg, state = _project(tmp_path)
    out = cli.ok("import", NODE_ID, "--config", cfg, "--state", state).output
    assert "imported" in out

    plan = cli.ok("plan", cfg, "--state", state).output
    assert "1 unchanged" in plan
    assert "noop" in plan


def test_a_resource_that_does_not_exist_fails_without_writing(tmp_path: Path) -> None:
    """Exit 1, and nothing recorded — a CI job branches on this."""
    cfg, state = _project(tmp_path, exists=False)
    assert "not found" in cli.fails("import", NODE_ID, "--config", cfg, "--state", state).output

    assert "1 to add" in cli.ok("plan", cfg, "--state", state).output


def test_a_dry_run_writes_nothing(tmp_path: Path) -> None:
    cfg, state = _project(tmp_path)
    out = cli.ok("import", NODE_ID, "--config", cfg, "--state", state, "--dry-run").output
    assert "would import" in out.lower()
    assert "1 to add" in cli.ok("plan", cfg, "--state", state).output


def test_importing_a_tracked_node_twice_is_refused_then_forced(tmp_path: Path) -> None:
    cfg, state = _project(tmp_path)
    cli.ok("import", NODE_ID, "--config", cfg, "--state", state)

    assert "tracked" in cli.ok("import", NODE_ID, "--config", cfg, "--state", state).output
    assert (
        "imported" in cli.ok("import", NODE_ID, "--config", cfg, "--state", state, "--force").output
    )


def test_an_id_for_a_name_addressed_type_is_refused(tmp_path: Path) -> None:
    """The local provider locates a file by its path, so an id is a
    misunderstanding — and one worth naming rather than ignoring."""
    cfg, state = _project(tmp_path)
    out = cli.fails("import", NODE_ID, "some-id", "--config", cfg, "--state", state).output
    assert "takes no id" in out


def test_a_pattern_matching_nothing_names_the_mistake(tmp_path: Path) -> None:
    cfg, state = _project(tmp_path)
    assert (
        "matched no resource"
        in cli.fails("import", "local.File:nope", "--config", cfg, "--state", state).output
    )


def test_an_id_cannot_be_shared_across_a_multi_node_match(tmp_path: Path) -> None:
    """One id names one resource. Spreading it over a glob would bind several
    nodes to the same physical thing."""
    target_a, target_b = tmp_path / "a.txt", tmp_path / "b.txt"
    target_a.write_text("a\n")
    target_b.write_text("b\n")
    cfg = tmp_path / "infra.py"
    cfg.write_text(
        "from atlantide.providers.local import File\n"
        f"File('a', path={str(target_a)!r}, content='a\\n')\n"
        f"File('b', path={str(target_b)!r}, content='b\\n')\n"
    )
    out = cli.fails(
        "import", "*", "an-id", "--config", cfg, "--state", tmp_path / "state.db"
    ).output
    assert "one at a time" in out.replace("\n", " ").replace("  ", " ")


# -- the listing --------------------------------------------------------------


def test_no_arguments_lists_what_could_be_imported(tmp_path: Path) -> None:
    cfg, state = _project(tmp_path)
    out = cli.ok("import", "--config", cfg, "--state", state).output
    assert "greeting" in out
    assert "1 resource(s) declared but not in state" in out.replace("\n", "")


def test_the_listing_is_empty_once_everything_is_tracked(tmp_path: Path) -> None:
    cfg, state = _project(tmp_path)
    cli.ok("import", NODE_ID, "--config", cfg, "--state", state)
    out = cli.ok("import", "--config", cfg, "--state", state).output
    assert "already tracks every resource" in out.replace("\n", "")


# -- json ---------------------------------------------------------------------


def test_json_reports_each_node(tmp_path: Path) -> None:
    cfg, state = _project(tmp_path)
    payload = json.loads(
        cli.ok("import", NODE_ID, "--config", cfg, "--state", state, "--json").output
    )
    assert payload["ok"] is True
    assert payload["imported"] == 1
    assert payload["refused"] == 0
    [node] = payload["nodes"]
    assert node["node_id"] == NODE_ID
    assert node["status"] == "imported"


def test_json_failure_is_still_a_document(tmp_path: Path) -> None:
    """A CI parser has to be able to read the failing case — it is the one it
    most needs to understand."""
    cfg, state = _project(tmp_path, exists=False)
    result = cli.fails("import", NODE_ID, "--config", cfg, "--state", state, "--json")
    payload = json.loads(result.output)
    assert payload["refused"] == 1
    assert payload["nodes"][0]["status"] == "not_found"


def test_the_listing_has_a_json_shape_too(tmp_path: Path) -> None:
    cfg, state = _project(tmp_path)
    payload = json.loads(cli.ok("import", "--config", cfg, "--state", state, "--json").output)
    assert [n["node_id"] for n in payload["importable"]] == [NODE_ID]
    assert payload["importable"][0]["identity_field"] is None


def test_the_report_says_how_much_of_the_resource_was_checked(tmp_path: Path) -> None:
    """ "imported" asserts the live resource matches config, and that claim is only
    as wide as the provider's read. The local File read reports a checksum and a
    path but never `content`, so the verdict covers one input of two — and the
    line has to say so without needing -v."""
    cfg, state = _project(tmp_path)
    out = cli.ok("import", NODE_ID, "--config", cfg, "--state", state).output
    assert "1 of 2 inputs checked" in out

    verbose = cli.ok("import", NODE_ID, "--config", cfg, "--state", state, "--force", "-v").output
    assert "not checked: content" in verbose
