"""CLI smoke tests via typer's runner."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from atlantide.cli.main import app

runner = CliRunner()


def _write_config(tmp: Path, content: str = "hi") -> Path:
    target = tmp / "out.txt"
    cfg = tmp / "config.py"
    cfg.write_text(
        "from atlantide.providers.local import File\n"
        f"File('f', path={str(target)!r}, content={content!r})\n"
    )
    return cfg


def test_outputs_surface_in_plan_and_report(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    cfg = tmp_path / "config.py"
    cfg.write_text(
        "from atlantide.core import output\n"
        "from atlantide.providers.local import File\n"
        f"f = File('f', path={str(target)!r}, content='hi')\n"
        "output('checksum', f.checksum)\n"  # a Ref -> resolved at apply
        "output('note', 'v1')\n"  # a literal
    )
    state = tmp_path / "state.db"

    plan = runner.invoke(app, ["plan", str(cfg), "--state", str(state)])
    assert plan.exit_code == 0, plan.output
    assert "Outputs:" in plan.output
    assert "default:checksum" in plan.output
    assert "known after apply" in plan.output  # the Ref output
    assert "'v1'" in plan.output  # the literal output

    apply = runner.invoke(app, ["apply", str(cfg), "--state", str(state), "--confirm"])
    assert apply.exit_code == 0, apply.output
    assert "Outputs:" in apply.output
    assert "default:note = v1" in apply.output
    assert "default:checksum = " in apply.output  # resolved to the real checksum


def test_plan_apply_destroy(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    state = tmp_path / "state.db"
    out = tmp_path / "out.txt"

    plan = runner.invoke(app, ["plan", str(cfg), "--state", str(state)])
    assert plan.exit_code == 0, plan.output
    assert "create" in plan.output

    apply = runner.invoke(app, ["apply", str(cfg), "--state", str(state), "-y"])
    assert apply.exit_code == 0, apply.output
    assert out.read_text() == "hi"
    assert "Applied: 1 to add" in apply.output

    # second apply -> nothing actionable, short-circuits before the report
    again = runner.invoke(app, ["apply", str(cfg), "--state", str(state), "-y"])
    assert again.exit_code == 0
    assert "nothing to apply" in again.output

    destroy = runner.invoke(app, ["destroy", "--state", str(state), "-y"])
    assert destroy.exit_code == 0, destroy.output
    assert "destroy" in destroy.output  # preview lists what will go
    assert "Destroyed: 1 resource(s)" in destroy.output
    assert not out.exists()


def _failing_config(tmp_path: Path) -> tuple[Path, str]:
    """A config whose File write fails: its parent path is a regular file."""
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    target = blocker / "child.txt"
    cfg = tmp_path / "config.py"
    cfg.write_text(
        "from atlantide.providers.local import File\n"
        f"File('f', path={str(target)!r}, content='hi')\n"
    )
    return cfg, "default:local.File:f"


def test_apply_failure_names_the_node_and_op(tmp_path: Path) -> None:
    cfg, node = _failing_config(tmp_path)
    state = tmp_path / "state.db"
    result = runner.invoke(app, ["apply", str(cfg), "--state", str(state), "-y"])
    assert result.exit_code == 1
    # the failing resource + op are surfaced, not just a bare provider message
    assert node in result.output
    assert "op=create" in result.output


def test_debug_flag_adds_a_traceback(tmp_path: Path) -> None:
    cfg, _ = _failing_config(tmp_path)
    state = tmp_path / "state.db"
    plain = runner.invoke(app, ["apply", str(cfg), "--state", str(state), "-y"])
    debug = runner.invoke(app, ["--debug", "apply", str(cfg), "--state", str(state), "-y"])
    assert debug.exit_code == 1
    assert "Traceback" in debug.output
    assert "Traceback" not in plain.output  # off by default


def test_destroy_previews_before_prompt(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    state = tmp_path / "state.db"
    runner.invoke(app, ["apply", str(cfg), "--state", str(state), "-y"])
    # answer "n": preview shown, prompt asked, nothing destroyed
    result = runner.invoke(app, ["destroy", "--state", str(state)], input="n\n")
    assert result.exit_code != 0  # aborted
    assert "- destroy" in result.output and "local.File:f" in result.output
    assert "Destroy these 1 resource(s)?" in result.output


def test_apply_prompts_and_aborts_on_no(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    state = tmp_path / "state.db"
    out = tmp_path / "out.txt"
    # answer "n" to the confirmation prompt
    result = runner.invoke(app, ["apply", str(cfg), "--state", str(state)], input="n\n")
    assert result.exit_code != 0  # typer aborts
    assert "Apply these changes?" in result.output
    assert not out.exists()  # nothing applied


def test_apply_prompts_and_proceeds_on_yes(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    state = tmp_path / "state.db"
    out = tmp_path / "out.txt"
    result = runner.invoke(app, ["apply", str(cfg), "--state", str(state)], input="y\n")
    assert result.exit_code == 0, result.output
    assert out.read_text() == "hi"


def test_apply_dry_run_makes_no_changes(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    state = tmp_path / "state.db"
    out = tmp_path / "out.txt"

    result = runner.invoke(app, ["apply", str(cfg), "--state", str(state), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "create" in result.output
    assert "dry run" in result.output
    assert not out.exists()  # nothing was actually created


def test_plan_on_invalid_config_errors(tmp_path: Path) -> None:
    cfg = tmp_path / "bad.py"
    cfg.write_text("import os\n")  # non-allowlisted import
    result = runner.invoke(app, ["plan", str(cfg), "--state", str(tmp_path / "s.db")])
    assert result.exit_code == 1
    assert "error" in result.output


def test_diagnostic_shows_source_snippet_and_caret(tmp_path: Path) -> None:
    cfg = tmp_path / "bad.py"
    cfg.write_text("x = 1\nwhile True:\n    pass\n")
    result = runner.invoke(app, ["plan", str(cfg), "--state", str(tmp_path / "s.db")])
    assert result.exit_code == 1
    assert "while True:" in result.output  # the offending source line
    assert "^" in result.output  # the caret
    assert "(line 2" in result.output  # the position


def test_graph_mermaid_boxes_each_stack(tmp_path: Path) -> None:
    cfg = tmp_path / "config.py"
    cfg.write_text(
        "from atlantide.core import Stack\n"
        "from atlantide.providers.local import File\n"
        "for env in ('dev', 'prod'):\n"
        "    with Stack(env, region='us-east-1'):\n"
        "        File('f', path=f'/tmp/{env}.txt', content='x')\n"
    )
    result = runner.invoke(app, ["graph", str(cfg), "--format", "mermaid"])
    assert result.exit_code == 0, result.output
    assert 'subgraph cluster0["dev"]' in result.output
    assert 'subgraph cluster1["prod"]' in result.output
    assert result.output.count("subgraph") == 2
    assert result.output.count("end") >= 2
    # node label drops the stack prefix (the box already names it)
    assert '["local.File:f"]' in result.output


def test_build_verify_deploy_roundtrip(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    art = tmp_path / "app.atlas"
    state = tmp_path / "state.db"
    out = tmp_path / "out.txt"

    built = runner.invoke(app, ["build", str(cfg), "-o", str(art)])
    assert built.exit_code == 0, built.output
    assert art.exists() and "built" in built.output

    verified = runner.invoke(app, ["verify", str(art)])
    assert verified.exit_code == 0, verified.output
    assert "verified" in verified.output

    # deploy from the artifact alone — no config path passed
    deployed = runner.invoke(app, ["deploy", str(art), "--state", str(state), "-y"])
    assert deployed.exit_code == 0, deployed.output
    assert out.read_text() == "hi"
    assert "Applied: 1 to add" in deployed.output


def test_verify_corrupted_artifact_errors(tmp_path: Path) -> None:
    art = tmp_path / "bad.atlas"
    art.write_text("{ not valid json")
    result = runner.invoke(app, ["verify", str(art)])
    assert result.exit_code == 1
    assert "error" in result.output


def test_live_apply_callback_drives_table() -> None:
    from atlantide.cli.progress import live_apply
    from atlantide.reconcile import Action

    # pre-seeded row + a lazily-added one; callback must not error on any phase
    with live_apply([("dev:local.File:a", Action.CREATE)]) as progress:
        progress("dev:local.File:a", Action.CREATE, "start")
        progress("dev:local.File:a", Action.CREATE, "finish")
        progress("dev:local.File:b", Action.UPDATE, "start")  # lazy row
        progress("dev:local.File:b", Action.UPDATE, "fail")


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "atlantide" in result.output


def test_plan_groups_by_stack_and_summary(tmp_path: Path) -> None:
    cfg = tmp_path / "config.py"
    cfg.write_text(
        "from atlantide.core import Stack\n"
        "from atlantide.providers.local import File\n"
        "for env in ('dev', 'prod'):\n"
        "    with Stack(env, region='us-east-1'):\n"
        "        File('f', path=f'/tmp/{env}.txt', content='x')\n"
    )
    result = runner.invoke(app, ["plan", str(cfg), "--state", str(tmp_path / "s.db")])
    assert result.exit_code == 0, result.output
    assert "dev" in result.output and "prod" in result.output  # stack group headers
    assert "Plan: 2 to add" in result.output
    assert "local.File:f" in result.output  # stack prefix dropped from the row


def test_plan_shows_field_diffs(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path, content="v1")
    state = tmp_path / "state.db"
    runner.invoke(app, ["apply", str(cfg), "--state", str(state), "-y"])
    # change the mutable content -> re-plan shows old -> new
    cfg2 = _write_config(tmp_path, content="v2")
    result = runner.invoke(app, ["plan", str(cfg2), "--state", str(state)])
    assert result.exit_code == 0, result.output
    assert "content:" in result.output and "→" in result.output
    assert "'v1'" in result.output and "'v2'" in result.output


def test_plan_json_output(tmp_path: Path) -> None:
    import json as _json

    cfg = _write_config(tmp_path)
    result = runner.invoke(app, ["plan", str(cfg), "--state", str(tmp_path / "s.db"), "--json"])
    assert result.exit_code == 0, result.output
    data = _json.loads(result.output)
    assert data["summary"]["create"] == 1
    assert data["changes"][0]["action"] == "create"
    assert data["blocked"] is False


def test_plan_detailed_exitcode(tmp_path: Path) -> None:
    cfg = _write_config(tmp_path)
    state = tmp_path / "state.db"
    # changes pending -> exit 2
    pending = runner.invoke(app, ["plan", str(cfg), "--state", str(state), "--detailed-exitcode"])
    assert pending.exit_code == 2
    runner.invoke(app, ["apply", str(cfg), "--state", str(state), "-y"])
    # nothing pending -> exit 0
    clean = runner.invoke(app, ["plan", str(cfg), "--state", str(state), "--detailed-exitcode"])
    assert clean.exit_code == 0


def test_plan_exits_nonzero_on_mandatory_policy_deny(tmp_path: Path) -> None:
    cfg = tmp_path / "config.py"
    # require-tags is mandatory; a taggable resource with no tags is denied
    cfg.write_text(
        "from atlantide.core import Stack\n"
        "from atlantide.policy import enforce\n"
        "from atlantide.providers.aws import S3Bucket\n"
        "enforce('require-tags')\n"
        "with Stack('dev', region='us-east-1'):\n"
        "    S3Bucket('b', bucket='no-tags-bucket')\n"
    )
    result = runner.invoke(app, ["plan", str(cfg), "--state", str(tmp_path / "s.db")])
    assert result.exit_code == 1
    assert "DENY" in result.output


def test_resources_lists_types() -> None:
    result = runner.invoke(app, ["resources"])
    assert result.exit_code == 0, result.output
    assert "aws.S3Bucket" in result.output
    assert "local.File" in result.output


def test_schema_shows_fields() -> None:
    result = runner.invoke(app, ["schema", "aws.S3Bucket"])
    assert result.exit_code == 0, result.output
    assert "bucket" in result.output
    assert "immutable" in result.output
    assert "computed" in result.output


def test_schema_unknown_type_suggests_available() -> None:
    result = runner.invoke(app, ["schema", "aws.Nope"])
    assert result.exit_code == 1
    assert "unknown type" in result.output
    assert "aws.S3Bucket" in result.output  # suggestion list


def test_project_config_supplies_defaults(tmp_path: Path, monkeypatch) -> None:
    cfg = _write_config(tmp_path)
    (tmp_path / "atlantide.toml").write_text(f'config = {cfg.name!r}\nstate = "infra.db"\n')
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["plan"])  # no config/state flags
    assert result.exit_code == 0, result.output
    assert "create" in result.output


def test_plan_without_config_or_project_errors(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["plan"])
    assert result.exit_code == 1
    assert "no config given" in result.output


def test_secret_set_get_list_rm_roundtrip(tmp_path: Path) -> None:
    state = str(tmp_path / "s.db")

    st = runner.invoke(app, ["secret", "set", "app/key", "hunter2", "--state", state])
    assert st.exit_code == 0

    listed = runner.invoke(app, ["secret", "list", "--state", state])
    assert listed.exit_code == 0
    assert "app/key" in listed.output
    assert "hunter2" not in listed.output  # list never shows values

    # get requires --reveal
    guarded = runner.invoke(app, ["secret", "get", "app/key", "--state", state])
    assert guarded.exit_code == 1
    assert "hunter2" not in guarded.output

    revealed = runner.invoke(app, ["secret", "get", "app/key", "-r", "--state", state])
    assert revealed.exit_code == 0
    assert revealed.output.strip() == "hunter2"

    assert runner.invoke(app, ["secret", "rm", "app/key", "--state", state]).exit_code == 0
    missing = runner.invoke(app, ["secret", "get", "app/key", "-r", "--state", state])
    assert missing.exit_code == 1  # gone -> error, no traceback
