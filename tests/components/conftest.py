"""Helpers for component tests: build a throwaway local git repo to fetch from."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_DEFAULT_BODY = "VALUE = 1\n"


def _git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def make_repo(
    root: Path, *, body: str = _DEFAULT_BODY, subdir: str = "pkg", tag: str = "v1"
) -> str:
    """Create a git repo with ``<subdir>/__init__.py`` at ``tag``; return the commit sha."""
    pkg = root / subdir
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(body)
    _git("init", "-q", cwd=root)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A", cwd=root)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init", cwd=root)
    _git("tag", tag, cwd=root)
    return _git("rev-parse", "HEAD", cwd=root)


@pytest.fixture
def repo(tmp_path: Path) -> tuple[str, str]:
    """A local component repo. Returns ``(file_url, commit_sha)``; package is at ``pkg/``."""
    src = tmp_path / "repo"
    commit = make_repo(src)
    return f"file://{src}", commit
