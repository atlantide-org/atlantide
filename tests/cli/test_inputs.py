"""Config inputs: ``-var``, ``--var-file``, and ``[inputs]``.

The determinism guarantee is over *(config, inputs)*, not config alone. Two runs
with the same inputs must produce byte-identical IR; two runs with different ones
are supposed to differ. Both halves are asserted here, because the first is what
makes the artifact/hash story hold and the second is the whole point of the
feature.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import Cli

cli = Cli()


CONFIG = (
    "from atlantide.core import output\n"
    "from atlantide.providers.local import File\n"
    "env = atlantide.input('env', 'dev')\n"
    "File('f', path=f'{atlantide.input(\"dir\")}/{env}.txt', content=env)\n"
    "output('env', env)\n"
)


def _config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.py"
    cfg.write_text(CONFIG)
    return cfg


def _plan(tmp_path: Path, *args: str) -> object:
    cfg = _config(tmp_path)
    return cli.run("plan", cfg, "--state", tmp_path / "s.db", "-var", f"dir={tmp_path}", *args)


# -- sources and precedence ---------------------------------------------------


def test_a_var_flag_reaches_the_config(tmp_path: Path) -> None:
    result = _plan(tmp_path, "-var", "env=prod")
    assert result.exit_code == 0, result.output
    assert "default:env = 'prod'" in result.output


def test_a_default_applies_when_nothing_is_passed(tmp_path: Path) -> None:
    result = _plan(tmp_path)
    assert result.exit_code == 0, result.output
    assert "default:env = 'dev'" in result.output


def test_a_required_input_with_no_value_names_how_to_supply_it(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    result = cli.run("plan", cfg, "--state", tmp_path / "s.db")
    assert result.exit_code == 1
    assert "required input 'dir'" in result.output
    assert "-var" in result.output


def test_a_var_file_supplies_inputs(tmp_path: Path) -> None:
    (tmp_path / "vars.toml").write_text(f'dir = "{tmp_path}"\nenv = "staging"\n')
    cfg = _config(tmp_path)
    result = cli.run(
        "plan",
        cfg,
        "--state",
        tmp_path / "s.db",
        "--var-file",
        tmp_path / "vars.toml",
    )
    assert "default:env = 'staging'" in result.output


def test_the_project_table_supplies_inputs(tmp_path: Path, monkeypatch) -> None:
    """The real first-hour need: one config, N environments, no flags."""
    (tmp_path / "atlantide.toml").write_text(f'[inputs]\ndir = "{tmp_path}"\nenv = "fromtoml"\n')
    cfg = _config(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = cli.run("plan", cfg, "--state", tmp_path / "s.db")
    assert "default:env = 'fromtoml'" in result.output


def test_a_profile_overlays_the_inputs_table(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "atlantide.toml").write_text(
        f'[inputs]\ndir = "{tmp_path}"\nenv = "dev"\n\n[profile.prod.inputs]\nenv = "prod"\n'
    )
    cfg = _config(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = cli.run("--profile", "prod", "plan", cfg, "--state", tmp_path / "s.db")
    assert "default:env = 'prod'" in result.output


def test_a_flag_beats_a_file_beats_the_project(tmp_path: Path, monkeypatch) -> None:
    """Most specific wins, so a one-off override needs no edit to a file."""
    (tmp_path / "atlantide.toml").write_text(f'[inputs]\ndir = "{tmp_path}"\nenv = "a"\n')
    (tmp_path / "vars.toml").write_text('env = "b"\n')
    cfg = _config(tmp_path)
    monkeypatch.chdir(tmp_path)

    from_file = cli.ok(
        "plan",
        cfg,
        "--state",
        tmp_path / "s.db",
        "--var-file",
        tmp_path / "vars.toml",
    )
    assert "default:env = 'b'" in from_file.output

    from_flag = cli.run(
        "plan",
        cfg,
        "--state",
        tmp_path / "s2.db",
        "--var-file",
        tmp_path / "vars.toml",
        "-var",
        "env=c",
    )
    assert "default:env = 'c'" in from_flag.output


# -- determinism --------------------------------------------------------------


def _built_hash(tmp_path: Path, name: str, *args: str) -> str:
    cfg = _config(tmp_path)
    out = tmp_path / f"{name}.atlas"
    cli.run("build", cfg, "-o", out, "-var", f"dir={tmp_path}", *args)
    return json.loads(out.read_text())["ir_hash"]


def test_the_same_inputs_produce_the_same_ir(tmp_path: Path) -> None:
    """The invariant the artifact story rests on."""
    first = _built_hash(tmp_path, "a", "-var", "env=prod")
    second = _built_hash(tmp_path, "b", "-var", "env=prod")
    assert first == second


def test_different_inputs_produce_different_ir(tmp_path: Path) -> None:
    """And the point of the feature: the config really did describe something
    else, so the hash has to say so."""
    assert _built_hash(tmp_path, "a", "-var", "env=dev") != _built_hash(
        tmp_path, "b", "-var", "env=prod"
    )


def test_an_input_nobody_read_does_not_change_the_plan(tmp_path: Path) -> None:
    """A stray variable set for another tool must not look like part of this
    plan's identity."""
    assert _built_hash(tmp_path, "a", "-var", "env=prod") == _built_hash(
        tmp_path, "b", "-var", "env=prod", "-var", "unrelated=whatever"
    )


# -- reporting ----------------------------------------------------------------


def test_the_plan_shows_what_it_was_evaluated_with(tmp_path: Path) -> None:
    """Otherwise "why is today's plan different" has no answer anywhere."""
    result = _plan(tmp_path, "-var", "env=prod")
    assert "inputs:" in result.output
    assert "env='prod'" in result.output.replace(" ", "").replace("\n", "")


def test_only_consumed_inputs_are_reported(tmp_path: Path) -> None:
    result = _plan(tmp_path, "-var", "env=prod", "-var", "unused=x")
    assert "unused" not in result.output


def test_plan_json_carries_the_inputs(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    result = cli.run(
        "plan",
        cfg,
        "--state",
        tmp_path / "s.db",
        "-var",
        f"dir={tmp_path}",
        "-var",
        "env=prod",
        "--json",
    )
    assert json.loads(result.stdout)["inputs"]["env"] == "prod"


# -- authoring mistakes -------------------------------------------------------


def test_a_malformed_var_is_reported(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    result = cli.run("plan", cfg, "--state", tmp_path / "s.db", "-var", "nonsense")
    assert result.exit_code == 1
    assert "name=value" in result.output


def test_an_unreadable_var_file_is_reported(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    result = cli.run(
        "plan",
        cfg,
        "--state",
        tmp_path / "s.db",
        "--var-file",
        tmp_path / "nope.toml",
    )
    assert result.exit_code == 1
    assert "cannot read --var-file" in result.output


def test_a_var_value_stays_a_string(tmp_path: Path) -> None:
    """A shell hands over text. Guessing between "2", 2 and True is how a config
    silently takes the wrong branch, so the conversion is the config's to make."""
    cfg = tmp_path / "typed.py"
    # `+ "!"` only succeeds if the value is already a string; an int would raise.
    cfg.write_text(
        "from atlantide.providers.local import File\n"
        f"File('f', path={str(tmp_path / 'out.txt')!r}, content=atlantide.input('n') + '!')\n"
    )
    cli.run("apply", cfg, "--state", tmp_path / "s.db", "-var", "n=2", "-y")
    assert (tmp_path / "out.txt").read_text() == "2!"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
