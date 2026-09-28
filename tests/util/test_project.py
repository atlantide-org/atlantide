"""`atlantide.util.project`: one definition of where the project file is.

The CLI and the local provider each carry a copy today; both must agree with
this one before they switch to it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atlantide.cli import project as cli_project
from atlantide.providers.local import paths as local_paths
from atlantide.util.project import PROJECT_FILENAME, find_project_file


def test_the_filename_matches_both_existing_copies() -> None:
    assert PROJECT_FILENAME == "atlantide.toml"
    assert PROJECT_FILENAME == cli_project.PROJECT_FILENAME
    assert PROJECT_FILENAME == local_paths.PROJECT_FILENAME


def test_finds_the_file_from_a_subdirectory(tmp_path: Path) -> None:
    (tmp_path / PROJECT_FILENAME).write_text("")
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    found = find_project_file(sub)
    assert found == (tmp_path / PROJECT_FILENAME).resolve()
    assert found == cli_project.find_project_file(sub)
    assert local_paths.PathScope.discover(sub).root == found.parent


def test_defaults_to_the_working_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / PROJECT_FILENAME).write_text("")
    monkeypatch.chdir(tmp_path)
    assert find_project_file() == (tmp_path / PROJECT_FILENAME).resolve()
    assert find_project_file() == cli_project.find_project_file()


def test_agrees_with_the_cli_when_there_is_none(tmp_path: Path) -> None:
    assert find_project_file(tmp_path) == cli_project.find_project_file(tmp_path)
