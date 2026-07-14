"""``atlantide component`` CLI: add / lock / vendor / verify, end to end.

Drives real git through a local ``file://`` repo, then proves a config can import
the vendored component and plan against it.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

import atlantide.components as components
from atlantide.cli.main import app

runner = CliRunner()

_COMPONENT = (
    "from atlantide.core import Component, child\n"
    "from atlantide.providers.local import Null\n\n\n"
    "class NullBox(Component):\n"
    "    def __init__(self, name, *, count=1):\n"
    "        self.thing = child(Null, 'thing', triggers={'n': str(count)})\n"
)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def component_repo(tmp_path: Path) -> str:
    """A local git repo whose ``nullbox/`` package holds a NullBox component."""
    repo = tmp_path / "repo"
    (repo / "nullbox").mkdir(parents=True)
    (repo / "nullbox" / "__init__.py").write_text(_COMPONENT)
    _git("init", "-q", cwd=repo)
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "init", cwd=repo)
    _git("tag", "v1", cwd=repo)
    return f"file://{repo}"


@pytest.fixture
def clean_mount() -> Iterator[None]:
    saved = list(components.__path__)
    yield
    components.__path__[:] = saved
    for name in list(sys.modules):
        if name.startswith("atlantide.components.") and name != "atlantide.components.lock":
            sys.modules.pop(name, None)


def test_add_lock_vendor_verify_then_plan(
    tmp_path: Path,
    component_repo: str,
    monkeypatch: pytest.MonkeyPatch,
    clean_mount: None,
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "atlantide.toml").write_text('config = "infra.py"\n')
    (project / "infra.py").write_text(
        "from atlantide.components.nullbox import NullBox\n\nNullBox('mybox', count=2)\n"
    )
    monkeypatch.chdir(project)

    # add: fetches, writes the toml source + lock, vendors the tree.
    add_args = ["component", "add", component_repo, "--ref", "v1", "--as", "nullbox"]
    added = runner.invoke(app, [*add_args, "--subdir", "nullbox"])
    assert added.exit_code == 0, added.output
    assert "atlantide.components.nullbox" in added.output
    lock_text = (project / "atlantide.lock").read_text()
    assert "[components.nullbox]" in lock_text and "sha256:" in lock_text
    assert (project / ".atlantis" / "components" / "nullbox" / "__init__.py").exists()
    assert "[components.nullbox]" in (project / "atlantide.toml").read_text()

    # verify: clean tree matches the lock.
    assert runner.invoke(app, ["component", "verify"]).exit_code == 0

    # tamper -> verify fails.
    vendored = project / ".atlantis" / "components" / "nullbox" / "__init__.py"
    vendored.write_text(_COMPONENT + "\n# tampered\n")
    tampered = runner.invoke(app, ["component", "verify"])
    assert tampered.exit_code == 1
    assert "tampered or drifted" in tampered.output

    # vendor: rematerializes clean from the lock, verify passes again.
    assert runner.invoke(app, ["component", "vendor"]).exit_code == 0
    assert runner.invoke(app, ["component", "verify"]).exit_code == 0

    # plan: the config imports the vendored component and gets its namespaced child.
    planned = runner.invoke(app, ["plan"])
    assert planned.exit_code == 0, planned.output
    assert "mybox-thing" in planned.output


def test_add_duplicate_alias_is_rejected(
    tmp_path: Path, component_repo: str, monkeypatch: pytest.MonkeyPatch, clean_mount: None
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "atlantide.toml").write_text(
        '[components.nullbox]\ngit = "x"\n'
    )
    result = runner.invoke(
        app, ["component", "add", component_repo, "--as", "nullbox", "--subdir", "nullbox"]
    )
    assert result.exit_code == 1
    assert "already declared" in result.output


def test_verify_without_lock_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["component", "verify"])
    assert result.exit_code == 1
    assert "no atlantide.lock" in result.output


def test_lock_resolves_declared_sources(
    tmp_path: Path, component_repo: str, monkeypatch: pytest.MonkeyPatch, clean_mount: None
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "atlantide.toml").write_text(
        f'[components.nullbox]\ngit = "{component_repo}"\nref = "v1"\nsubdir = "nullbox"\n'
    )
    result = runner.invoke(app, ["component", "lock"])
    assert result.exit_code == 0, result.output
    assert "locked nullbox" in result.output
    assert "[components.nullbox]" in (tmp_path / "atlantide.lock").read_text()
    # vendored, so a follow-up verify passes.
    assert runner.invoke(app, ["component", "verify"]).exit_code == 0


def test_default_alias_derived_from_url(
    tmp_path: Path, component_repo: str, monkeypatch: pytest.MonkeyPatch, clean_mount: None
) -> None:
    # repo dir is named "repo", so the derived alias is "repo".
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["component", "add", component_repo, "--subdir", "nullbox"])
    assert result.exit_code == 0, result.output
    assert "atlantide.components.repo" in result.output
