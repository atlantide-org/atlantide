"""The one-file project the ``state`` suites apply before administering its state."""

from __future__ import annotations

from pathlib import Path

from tests.support import Cli, write_config

cli = Cli()


def write_project(
    tmp_path: Path, *, protected: bool = False, with_output: bool = False
) -> tuple[Path, Path]:
    """A config declaring one ``local.File`` named ``f``, plus its state path.

    ``protected`` marks the file ``prevent_destroy``; ``with_output`` also
    declares its checksum as an output, so a snapshot has outputs to carry.
    """
    lifecycle = ", lifecycle=Lifecycle(prevent_destroy=True)" if protected else ""
    output = "output('checksum', f.checksum)\n" if with_output else ""
    cfg = write_config(
        tmp_path,
        "from atlantide.core import Lifecycle, output\n"
        "from atlantide.providers.local import File\n"
        f"f = File('f', path={str(tmp_path / 'out.txt')!r}, content='hi'{lifecycle})\n"
        f"{output}",
    )
    return cfg, tmp_path / "state.db"


def apply_project(
    tmp_path: Path, *, protected: bool = False, with_output: bool = False
) -> tuple[Path, Path]:
    """:func:`write_project`, applied."""
    cfg, state = write_project(tmp_path, protected=protected, with_output=with_output)
    cli.ok("apply", cfg, "--state", state, "-y")
    return cfg, state
