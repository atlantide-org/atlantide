"""``--env``: selecting one environment out of a config's ``Config``.

The load-bearing assertion here is
:func:`test_a_narrowed_plan_does_not_delete_the_other_environment`. Narrowing
makes the config declare nothing for the environments left out, so without
suppression every one of their nodes diffs as a delete — the flag an operator
reaches for to be *careful* would destroy the environment they were protecting.

Structured like ``test_inputs.py``, whose determinism contract this extends:
the guarantee is over *(config, inputs, selected environments)*.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import Cli

cli = Cli()


def _config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.py"
    cfg.write_text(
        "from atlantide.core import Config, Stack, var\n"
        "from atlantide.providers.local import File\n"
        "config = Config(\n"
        "    schema={'suffix': var(str, default='')},\n"
        "    envs={\n"
        "        'dev':  {'region': 'eu-north-1'},\n"
        "        'prod': {'region': 'us-east-1', 'suffix': '!'},\n"
        "    },\n"
        ")\n"
        "for env in config.envs():\n"
        "    with Stack(env.name, config=env):\n"
        f"        File('f', path=f'{tmp_path}/{{env.name}}.txt', "
        "content=env.name + env.suffix)\n"
    )
    return cfg


def _plan(tmp_path: Path, *args: str, state: str = "s.db") -> object:
    return cli.run("plan", _config(tmp_path), "--state", tmp_path / state, *args)


def _actions(tmp_path: Path, *args: str, state: str = "s.db") -> dict[str, str]:
    """``{node_id: action}`` from a ``--json`` plan."""
    result = cli.run("plan", _config(tmp_path), "--state", tmp_path / state, "--json", *args)
    assert result.exit_code == 0, result.output
    return {c["node_id"]: c["action"] for c in json.loads(result.stdout)["changes"]}


# -- selection ---------------------------------------------------------------


def test_no_env_flag_covers_every_environment(tmp_path: Path) -> None:
    assert _actions(tmp_path) == {
        "dev:local.File:f": "create",
        "prod:local.File:f": "create",
    }


def test_env_narrows_the_plan_to_one_environment(tmp_path: Path) -> None:
    """dev is not declared at all under `--env prod`, so it has nothing to plan."""
    assert _actions(tmp_path, "--env", "prod") == {"prod:local.File:f": "create"}


def test_env_is_repeatable(tmp_path: Path) -> None:
    result = cli.run("plan", _config(tmp_path), "--state", tmp_path / "s.db", "--json", "-e", "dev")
    assert json.loads(result.stdout)["envs"]["selected"] == ["dev"]


def test_an_unknown_environment_names_the_declared_ones(tmp_path: Path) -> None:
    """A typo must not read as a run that did what was asked."""
    result = _plan(tmp_path, "--env", "prd")
    assert result.exit_code == 1
    assert "unknown environment 'prd'" in result.output
    assert "dev, prod" in result.output


def test_env_against_a_config_with_no_config_is_reported(tmp_path: Path) -> None:
    cfg = tmp_path / "plain.py"
    cfg.write_text(
        "from atlantide.providers.local import File\n"
        f"File('f', path='{tmp_path}/x.txt', content='x')\n"
    )
    result = cli.run("plan", cfg, "--state", tmp_path / "s.db", "--env", "prod")
    assert result.exit_code == 1
    assert "declares no Config" in result.output


# -- the safety rule ---------------------------------------------------------


def test_a_narrowed_plan_does_not_delete_the_other_environment(tmp_path: Path) -> None:
    """`apply --env prod` must not destroy dev.

    dev's node stays in the changeset as a NOOP rather than vanishing, the same
    shape `--target` produces: a NOOP writes nothing, and keeping the node means
    its stored ``input_hash`` is left exactly as it was, so the next full run
    sees no spurious change.
    """
    cfg = _config(tmp_path)
    state = tmp_path / "s.db"
    cli.ok("apply", cfg, "--state", state, "-y")  # both environments exist
    cfg.write_text(cfg.read_text().replace("env.name + env.suffix", "'changed'"))

    result = cli.run("plan", cfg, "--state", state, "--json", "--env", "prod")
    actions = {c["node_id"]: c["action"] for c in json.loads(result.stdout)["changes"]}
    assert actions["dev:local.File:f"] == "noop", actions
    assert actions["prod:local.File:f"] == "update", actions

    cli.ok("apply", cfg, "--state", state, "--env", "prod", "-y")
    assert (tmp_path / "dev.txt").read_text() == "dev"  # untouched
    assert (tmp_path / "prod.txt").read_text() == "changed"


def _shared_config(tmp_path: Path, *, cross_stack_ref: bool) -> Path:
    """A config with a `common` stack outside the environment loop.

    With ``cross_stack_ref`` the environments consume ``common``'s output, which
    sends the registry through ``inline_stack_outputs``'s rebuild path.
    """
    cfg = tmp_path / f"shared-{cross_stack_ref}.py"
    cfg.write_text(
        "from atlantide.core import Config, Stack, output\n"
        "from atlantide.providers.local import File\n"
        "with Stack('common', region='us-east-1'):\n"
        f"    base = File('base', path='{tmp_path}/base.txt', content='base')\n"
        "    checksum = output('checksum', base.checksum)\n"
        "config = Config(envs={'dev': {'region': 'a'}, 'prod': {'region': 'b'}})\n"
        "for env in config.envs():\n"
        "    with Stack(env.name, config=env):\n"
        f"        File('f', path=f'{tmp_path}/{{env.name}}.txt', "
        f"content={'checksum' if cross_stack_ref else 'env.name'})\n"
    )
    return cfg


def test_a_stack_outside_the_environment_loop_is_untouched(tmp_path: Path) -> None:
    """Shared/base stacks are not in `envs_declared`, so narrowing skips them."""
    cfg = _shared_config(tmp_path, cross_stack_ref=False)
    result = cli.run("plan", cfg, "--state", tmp_path / "s.db", "--json", "--env", "prod")
    actions = {c["node_id"]: c["action"] for c in json.loads(result.stdout)["changes"]}
    assert actions["common:local.File:base"] == "create", actions
    assert "dev:local.File:f" not in actions, actions


def test_suppression_survives_an_in_config_cross_stack_reference(tmp_path: Path) -> None:
    """`inline_stack_outputs` rebuilds the registry when a config reads another
    stack's output, and every carried field has to survive that rebuild. Losing
    the selection here would disable suppression for exactly the configs that
    share a stack across environments — where a `common` stack lives."""
    cfg = _shared_config(tmp_path, cross_stack_ref=True)
    state = tmp_path / "s.db"
    cli.ok("apply", cfg, "--state", state, "-y")

    result = cli.run("plan", cfg, "--state", state, "--json", "--env", "prod")
    payload = json.loads(result.stdout)
    assert payload["envs"] == {"declared": ["dev", "prod"], "selected": ["prod"]}
    actions = {c["node_id"]: c["action"] for c in payload["changes"]}
    assert actions["dev:local.File:f"] == "noop", actions


def test_target_and_env_intersect_rather_than_fight(tmp_path: Path) -> None:
    assert _actions(tmp_path, "--env", "prod", "--target", "prod:*") == {
        "prod:local.File:f": "create"
    }


def test_a_target_excluded_by_env_says_so(tmp_path: Path) -> None:
    """Otherwise this reads as "--target matched nothing", pointing at the
    pattern rather than the flag that excluded it."""
    cfg = _config(tmp_path)
    state = tmp_path / "s.db"
    cli.ok("apply", cfg, "--state", state, "-y")
    result = cli.run("plan", cfg, "--state", state, "--env", "prod", "--target", "dev:*")
    assert result.exit_code == 1
    assert "--env excluded" in result.output


def test_destroy_env_is_a_stack_glob(tmp_path: Path) -> None:
    """`destroy` reads no config, so `--env dev` selects from state by stack."""
    cfg = _config(tmp_path)
    state = tmp_path / "s.db"
    cli.ok("apply", cfg, "--state", state, "-y")
    cli.ok("destroy", "--state", state, "--env", "dev", "-y")
    assert not (tmp_path / "dev.txt").exists()
    assert (tmp_path / "prod.txt").exists()


# -- determinism -------------------------------------------------------------


def _built_hash(tmp_path: Path, name: str, *args: str) -> str:
    out = tmp_path / f"{name}.atlas"
    cli.ok("build", _config(tmp_path), "-o", out, *args)
    return json.loads(out.read_text())["ir_hash"]


def test_the_same_selection_produces_the_same_ir(tmp_path: Path) -> None:
    assert _built_hash(tmp_path, "a", "--env", "prod") == _built_hash(
        tmp_path, "b", "--env", "prod"
    )


def test_a_different_selection_produces_different_ir(tmp_path: Path) -> None:
    """The config really did describe something else, so the hash has to say so."""
    assert _built_hash(tmp_path, "a", "--env", "dev") != _built_hash(tmp_path, "b", "--env", "prod")


def test_the_artifact_records_which_environment_built_it(tmp_path: Path) -> None:
    """`deploy` has no source to re-read; provenance is only knowable if the
    build wrote it down."""
    out = tmp_path / "prod.atlas"
    cli.ok("build", _config(tmp_path), "-o", out, "--env", "prod")
    assert json.loads(out.read_text())["envs"] == ["prod"]


# -- reporting ---------------------------------------------------------------


def test_the_plan_says_which_environments_it_left_out(tmp_path: Path) -> None:
    """A narrowed plan states what it excluded."""
    result = _plan(tmp_path, "--env", "prod")
    assert "envs:" in result.output
    assert "will not change" in result.output


def test_an_unnarrowed_plan_stays_quiet(tmp_path: Path) -> None:
    assert "envs:" not in _plan(tmp_path).output


def test_plan_json_carries_both_lists(tmp_path: Path) -> None:
    result = cli.run(
        "plan", _config(tmp_path), "--state", tmp_path / "s.db", "--json", "--env", "prod"
    )
    envs = json.loads(result.stdout)["envs"]
    assert envs == {"declared": ["dev", "prod"], "selected": ["prod"]}


def test_validate_checks_every_environment_by_default(tmp_path: Path) -> None:
    """A type error in a prod-only value fails the pull request, not the apply."""
    cfg = tmp_path / "broken.py"
    cfg.write_text(
        "from atlantide.core import Config, Stack, var\n"
        "config = Config(schema={'size': var(int, default=1)},\n"
        "                envs={'dev': {'region': 'a'}, 'prod': {'region': 'b', 'size': 'x'}})\n"
    )
    result = cli.run("validate", cfg)
    assert result.exit_code == 1
    assert "environment 'prod'" in result.output


def test_validate_names_the_environments_it_checked(tmp_path: Path) -> None:
    result = cli.ok("validate", _config(tmp_path))
    assert "2 environment(s) [dev, prod]" in result.output


def test_validate_json_carries_the_environments(tmp_path: Path) -> None:
    result = cli.ok("validate", _config(tmp_path), "--json", "--env", "dev")
    assert json.loads(result.stdout)["envs"] == {
        "declared": ["dev", "prod"],
        "selected": ["dev"],
    }


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
