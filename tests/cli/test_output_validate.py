"""``atlantide output`` and ``atlantide validate``.

Both exist for moments when the usual commands cannot help: reading a value back
when the config is mid-edit, and checking a config where `plan` would need a
state backend it should not be given.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import Cli

cli = Cli()


def _config(tmp_path: Path, *, sensitive: bool = False, stacks: bool = False) -> Path:
    cfg = tmp_path / "config.py"
    if stacks:
        cfg.write_text(
            "from atlantide.core import Stack, output\n"
            "from atlantide.providers.local import File\n"
            "for env in ('dev', 'prod'):\n"
            "    with Stack(env, region='eu-north-1'):\n"
            f"        f = File('f', path=f'{tmp_path}/{{env}}.txt', content=env)\n"
            "        output('checksum', f.checksum)\n"
        )
    elif sensitive:
        cfg.write_text(
            "from atlantide.core import output\n"
            "from atlantide.providers.random import Password\n"
            "p = Password('p', length=12)\n"
            "output('secret_value', p.result)\n"
            "output('plain', 'visible')\n"
        )
    else:
        cfg.write_text(
            "from atlantide.core import output\n"
            "from atlantide.providers.local import File\n"
            f"f = File('f', path={str(tmp_path / 'out.txt')!r}, content='hi')\n"
            "output('checksum', f.checksum)\n"
            "output('note', 'v1')\n"
        )
    return cfg


def _applied(tmp_path: Path, **kw: bool) -> Path:
    cfg = _config(tmp_path, **kw)
    state = tmp_path / "state.db"
    cli.run("apply", cfg, "--state", state, "-y")
    return state


# -- output -------------------------------------------------------------------


def test_output_prints_a_value_bare_so_it_pipes(tmp_path: Path) -> None:
    """`vpc=$(atlantide output vpc_id)` has to yield the value and nothing else —
    no banner, no label, no colour."""
    state = _applied(tmp_path)
    result = cli.run("output", "note", "--state", state)
    assert result.stdout.strip() == "v1"


def test_output_lists_everything_when_given_no_name(tmp_path: Path) -> None:
    state = _applied(tmp_path)
    result = cli.run("output", "--state", state)
    assert "default:note = v1" in result.output
    assert "default:checksum" in result.output


def test_output_reads_state_without_the_config(tmp_path: Path) -> None:
    """The reason it reads state and nothing else: a script needs the value most
    when the config is broken or half-edited."""
    cfg = _config(tmp_path)
    state = _applied(tmp_path)
    cfg.write_text("this is not valid python at all\n")

    result = cli.run("output", "note", "--state", state)
    assert result.stdout.strip() == "v1"


def test_an_unknown_output_lists_what_is_there(tmp_path: Path) -> None:
    state = _applied(tmp_path)
    result = cli.run("output", "nope", "--state", state)
    assert result.exit_code == 1
    assert "available:" in result.output
    assert "note" in result.output


def test_a_sensitive_output_needs_reveal(tmp_path: Path) -> None:
    """Same gate as `secret get`, for the same reason: this lands in scrollback
    and CI logs, which outlive the command."""
    state = _applied(tmp_path, sensitive=True)

    guarded = cli.run("output", "secret_value", "--state", state)
    assert guarded.exit_code == 1
    assert "sensitive" in guarded.output

    revealed = cli.run("output", "secret_value", "--state", state, "-r")
    assert len(revealed.stdout.strip()) == 12


def test_listing_redacts_sensitive_values(tmp_path: Path) -> None:
    state = _applied(tmp_path, sensitive=True)
    result = cli.run("output", "--state", state)
    assert "(sensitive)" in result.output
    assert "visible" in result.output, "non-sensitive values are still shown"


def test_a_name_exported_by_two_stacks_asks_which(tmp_path: Path) -> None:
    """Guessing would be worse than asking: the two values are different, and
    picking one silently is how a script deploys against the wrong environment."""
    state = _applied(tmp_path, stacks=True)

    ambiguous = cli.run("output", "checksum", "--state", state)
    assert ambiguous.exit_code == 1
    assert "several stacks" in ambiguous.output

    cli.run("output", "checksum", "--stack", "prod", "--state", state)


def test_output_json_is_one_document(tmp_path: Path) -> None:
    state = _applied(tmp_path)
    result = cli.run("output", "--state", state, "--json")
    assert json.loads(result.stdout)["outputs"]["default:note"] == "v1"


def test_output_of_empty_state_says_so(tmp_path: Path) -> None:
    result = cli.run("output", "--state", tmp_path / "empty.db")
    assert "no outputs recorded" in result.output


# -- validate -----------------------------------------------------------------


def test_validate_accepts_a_good_config(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    result = cli.run("validate", cfg)
    assert "ok" in result.output
    assert "1 resource(s)" in result.output


def test_validate_touches_no_state(tmp_path: Path) -> None:
    """The whole point: it can run in a pre-commit hook or on a pull request,
    where `plan` would need a backend it should not be handed."""
    cfg = _config(tmp_path)
    result = cli.run("validate", cfg)
    assert not list(tmp_path.glob("*.db")), "no state database was created"
    assert "state:" not in result.output


def test_validate_reports_a_bad_config_with_a_caret(tmp_path: Path) -> None:
    cfg = tmp_path / "bad.py"
    cfg.write_text("x = 1\nwhile True:\n    pass\n")
    result = cli.run("validate", cfg)
    assert result.exit_code == 1
    assert "while True:" in result.output
    assert "^" in result.output


def test_validate_catches_a_dependency_cycle(tmp_path: Path) -> None:
    """Beyond syntax: the graph has to be acyclic, which only the compile knows."""
    cfg = tmp_path / "cycle.py"
    cfg.write_text(
        "from atlantide.providers.local import File\n"
        "a = File('a', path='/tmp/a', content='x')\n"
        "b = File('b', path='/tmp/b', content=a.checksum)\n"
        "a.content = b.checksum\n"
    )
    result = cli.run("validate", cfg)
    assert result.exit_code == 1


def test_validate_json_is_one_document(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    result = cli.run("validate", cfg, "--json")
    assert json.loads(result.stdout)["resources"] == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
