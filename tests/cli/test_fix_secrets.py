"""Regression: `output --reveal` without a keyfile fails cleanly and mints no key."""

from __future__ import annotations

from pathlib import Path

from tests.support import Cli, write_config

cli = Cli()


def test_output_reveal_without_a_keyfile_fails_and_creates_none(tmp_path: Path) -> None:
    cfg = write_config(
        tmp_path,
        "from atlantide.core import output\n"
        "from atlantide.providers.random import Password\n"
        "p = Password('p', length=12)\n"
        "output('secret_value', p.result)\n",
    )
    state = tmp_path / "state.db"
    cli.ok("apply", cfg, "--state", state, "-y")
    keyfile = tmp_path / "atlantide.key"
    assert keyfile.exists()
    keyfile.unlink()

    result = cli.run("output", "secret_value", "--state", state, "--reveal")
    assert result.exit_code != 0
    assert "no keyfile" in result.output
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert not keyfile.exists(), "unsealing must not create a replacement keyfile"
