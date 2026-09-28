"""A text plan folds unchanged rows into a per-stack count.

In a large config nearly every row of a re-plan is a NOOP, and listing them all
buries the handful that change. The rows are hidden by default, counted per stack
and in the summary, listed again with ``--show-unchanged``, and always present
in ``--json``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import Cli

cli = Cli()

CONFIG = """
from atlantide.providers.local import File

File('a', path='a.txt', content='same')
File('b', path='b.txt', content={content!r})
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A project with both files applied, then ``b`` edited so a re-plan has one
    UPDATE and one NOOP."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "atlantide.toml").write_text('state = "s.db"\n')
    cfg = tmp_path / "infra.py"
    cfg.write_text(CONFIG.format(content="v1"))
    state = tmp_path / "s.db"
    cli.ok("apply", cfg, "--state", state, "-y")
    cfg.write_text(CONFIG.format(content="v2"))
    return cfg, state


def test_unchanged_rows_are_folded_by_default(project: tuple[Path, Path]) -> None:
    cfg, state = project
    out = cli.run("plan", cfg, "--state", state).output
    assert "update" in out and "local.File:b" in out
    assert "noop" not in out
    assert "local.File:a" not in out
    assert "  1 unchanged" in out
    # The summary still counts them.
    assert "1 to change, 1 unchanged" in out


def test_show_unchanged_lists_them(project: tuple[Path, Path]) -> None:
    cfg, state = project
    out = cli.run("plan", cfg, "--state", state, "--show-unchanged").output
    assert "= noop" in out and "local.File:a" in out
    assert "1 to change, 1 unchanged" in out


def test_apply_folds_the_plan_it_shows_the_same_way(project: tuple[Path, Path]) -> None:
    cfg, state = project
    folded = cli.ok("apply", cfg, "--state", state, "--dry-run").output
    assert "noop" not in folded and "  1 unchanged" in folded
    listed = cli.ok("apply", cfg, "--state", state, "--dry-run", "--show-unchanged").output
    assert "= noop" in listed


def test_json_always_carries_the_noop_rows(project: tuple[Path, Path]) -> None:
    cfg, state = project
    data = json.loads(cli.run("plan", cfg, "--state", state, "--json").output)
    actions = {change["node_id"]: change["action"] for change in data["changes"]}
    assert actions["default:local.File:a"] == "noop"
    assert actions["default:local.File:b"] == "update"


def test_a_stack_with_nothing_to_do_is_one_line(project: tuple[Path, Path]) -> None:
    cfg, state = project
    cli.ok("apply", cfg, "--state", state, "-y")
    out = cli.ok("plan", cfg, "--state", state).output
    assert "  2 unchanged" in out
    assert "noop" not in out
