"""Config writers shared across the CLI suites."""

from __future__ import annotations

from pathlib import Path

from tests.support import write_config


def file_config(directory: Path, content: str = "hi") -> Path:
    """One ``local.File`` named ``f`` at ``directory / "out.txt"`` holding ``content``."""
    return write_config(
        directory,
        "from atlantide.providers.local import File\n"
        f"File('f', path={str(directory / 'out.txt')!r}, content={content!r})\n",
    )
