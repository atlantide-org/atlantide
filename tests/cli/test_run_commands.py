"""``plan``, ``apply``, ``destroy`` and ``refresh`` end to end, through typer's runner."""

from __future__ import annotations

from pathlib import Path

from tests.cli.conftest import file_config
from tests.support import Cli

cli = Cli()


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

    plan = cli.ok("plan", cfg, "--state", state)
    assert "Outputs:" in plan.output
    assert "default:checksum" in plan.output
    assert "known after apply" in plan.output  # the Ref output
    assert "'v1'" in plan.output  # the literal output

    apply = cli.ok("apply", cfg, "--state", state, "--confirm")
    assert "Outputs:" in apply.output
    assert "default:note = v1" in apply.output
    assert "default:checksum = " in apply.output  # resolved to the real checksum


def test_plan_apply_destroy(tmp_path: Path) -> None:
    cfg = file_config(tmp_path)
    state = tmp_path / "state.db"
    out = tmp_path / "out.txt"

    plan = cli.ok("plan", cfg, "--state", state)
    assert "create" in plan.output

    apply = cli.ok("apply", cfg, "--state", state, "-y")
    assert out.read_text() == "hi"
    assert "Applied: 1 to add" in apply.output

    # second apply -> nothing actionable, short-circuits before the report
    again = cli.ok("apply", cfg, "--state", state, "-y")
    assert "nothing to apply" in again.output

    destroy = cli.ok("destroy", "--state", state, "-y")
    assert "destroy" in destroy.output  # preview lists what will go
    assert "Destroyed: 1 resource(s)" in destroy.output
    assert not out.exists()


def test_apply_dry_run_makes_no_changes(tmp_path: Path) -> None:
    cfg = file_config(tmp_path)
    state = tmp_path / "state.db"
    out = tmp_path / "out.txt"

    result = cli.run("apply", cfg, "--state", state, "--dry-run")
    assert "create" in result.output
    assert "dry run" in result.output
    assert not out.exists()  # nothing was created


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
    result = cli.run("--version")
    assert "atlantide" in result.output


def test_plan_groups_by_stack_and_summary(tmp_path: Path) -> None:
    cfg = tmp_path / "config.py"
    cfg.write_text(
        "from atlantide.core import Stack\n"
        "from atlantide.providers.local import File\n"
        "for env in ('dev', 'prod'):\n"
        "    with Stack(env, region='us-east-1'):\n"
        "        File('f', path=f'{env}.txt', content='x')\n"
    )
    result = cli.run("plan", cfg, "--state", tmp_path / "s.db")
    assert "dev" in result.output and "prod" in result.output  # stack group headers
    assert "Plan: 2 to add" in result.output
    assert "local.File:f" in result.output  # stack prefix dropped from the row


def test_plan_shows_field_diffs(tmp_path: Path) -> None:
    cfg = file_config(tmp_path, content="v1")
    state = tmp_path / "state.db"
    cli.ok("apply", cfg, "--state", state, "-y")
    # change the mutable content -> re-plan shows old -> new
    cfg2 = file_config(tmp_path, content="v2")
    result = cli.run("plan", cfg2, "--state", state)
    assert "content:" in result.output and "→" in result.output
    assert "'v1'" in result.output and "'v2'" in result.output


def test_plan_json_output(tmp_path: Path) -> None:
    import json as _json

    cfg = file_config(tmp_path)
    result = cli.run("plan", cfg, "--state", tmp_path / "s.db", "--json")
    data = _json.loads(result.output)
    assert data["summary"]["create"] == 1
    assert data["changes"][0]["action"] == "create"
    assert data["blocked"] is False


def test_plan_detailed_exitcode(tmp_path: Path) -> None:
    cfg = file_config(tmp_path)
    state = tmp_path / "state.db"
    # changes pending -> exit 2
    pending = cli.run("plan", cfg, "--state", state, "--detailed-exitcode")
    assert pending.exit_code == 2
    cli.ok("apply", cfg, "--state", state, "-y")
    # nothing pending -> exit 0
    cli.run("plan", cfg, "--state", state, "--detailed-exitcode")


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
    result = cli.run("plan", cfg, "--state", tmp_path / "s.db")
    assert result.exit_code == 1
    assert "DENY" in result.output


def test_refresh_says_how_much_of_each_resource_it_checked(tmp_path: Path) -> None:
    """The report must not claim more than the provider's read established.

    `local.File` declares `path` and `content` as inputs but its read reports
    `path` and `checksum`, so `content` is never checked. Guards against an
    unchecked field and a verified one both rendering as a bare "in sync".
    """
    cfg = file_config(tmp_path)
    state = tmp_path / "state.db"
    assert cli.run("apply", cfg, "--state", state, "-y").exit_code == 0

    result = cli.run("refresh", "--state", state)
    assert "inputs checked" in result.output
    assert "pass --verbose" in result.output
    # The claim is scoped, not absolute.
    assert "state matches reality" not in result.output

    verbose = cli.ok("refresh", "--state", state, "-v")
    assert "not checked:" in verbose.output
    assert "content" in verbose.output
    assert "pass --verbose" not in verbose.output  # already listed


def test_refresh_json_carries_the_coverage_of_each_verdict(tmp_path: Path) -> None:
    import json

    cfg = file_config(tmp_path)
    state = tmp_path / "state.db"
    assert cli.run("apply", cfg, "--state", state, "-y").exit_code == 0

    result = cli.run("refresh", "--state", state, "--json")
    payload = json.loads(result.output)
    node = payload["nodes"][0]
    assert node["kind"] == "in_sync"
    assert "content" in node["unobserved"]
    assert "path" in node["observed"]


def _seed_a_second_writer(state: Path, path: Path) -> None:
    """Write a state row the way a concurrent run would, between plan and apply."""
    from atlantide.state import SqliteStateBackend, StateNode

    backend = SqliteStateBackend(str(state))
    backend.put(
        StateNode(
            id="default:local.File:g",
            type="local.File",
            provider="local",
            provider_version="1.0.0",
            input_hash="written-by-another-run",
            outputs={"checksum": "x", "path": str(path)},
            properties={"path": str(path), "content": "other"},
        )
    )
    backend.close()


def test_apply_refuses_when_state_moved_since_the_plan_was_shown(
    tmp_path: Path, monkeypatch
) -> None:
    """The apply re-diffs under the lock, so what runs can differ from what was
    approved. Doing that silently would apply an unreviewed change.

    The window is inside one `apply` invocation, between the plan it renders and
    the lock it then takes, so the concurrent write is injected at the
    confirmation prompt, which sits in that window.
    """
    cfg = tmp_path / "two.py"
    cfg.write_text(
        "from atlantide.providers.local import File\n"
        f"File('f', path={str(tmp_path / 'f.txt')!r}, content='hi')\n"
        f"File('g', path={str(tmp_path / 'g.txt')!r}, content='yo')\n"
    )
    state = tmp_path / "state.db"

    def confirm_then_race(*_args: object, **_kw: object) -> None:
        _seed_a_second_writer(state, tmp_path / "g.txt")

    monkeypatch.setattr("atlantide.cli.commands.run.require_confirm", confirm_then_race)

    result = cli.run("apply", cfg, "--state", state, "-y")
    assert result.exit_code == 1
    assert "no longer the ones shown" in result.output
    assert "Re-run" in result.output


def test_allow_plan_drift_opts_back_into_applying_the_fresh_plan(
    tmp_path: Path, monkeypatch
) -> None:
    cfg = tmp_path / "two.py"
    cfg.write_text(
        "from atlantide.providers.local import File\n"
        f"File('f', path={str(tmp_path / 'f.txt')!r}, content='hi')\n"
        f"File('g', path={str(tmp_path / 'g.txt')!r}, content='yo')\n"
    )
    state = tmp_path / "state.db"

    def confirm_then_race(*_args: object, **_kw: object) -> None:
        _seed_a_second_writer(state, tmp_path / "g.txt")

    monkeypatch.setattr("atlantide.cli.commands.run.require_confirm", confirm_then_race)

    cli.run("apply", cfg, "--state", state, "-y", "--allow-plan-drift")
    assert (tmp_path / "g.txt").read_text() == "yo"


def test_refresh_write_does_not_delete_a_row_it_could_not_read(tmp_path: Path) -> None:
    """A resource the provider cannot find is reported, not forgotten.

    `local.File` reads MISSING when the file is gone. Deleting the row on that
    evidence would mean a single bad read — an unpaginated listing, a missing
    permission — permanently loses the only record that the resource exists, and
    the next apply builds a second one.
    """
    cfg = file_config(tmp_path)
    state = tmp_path / "state.db"
    assert cli.run("apply", cfg, "--state", state, "-y").exit_code == 0
    (tmp_path / "out.txt").unlink()  # the provider can no longer see it

    result = cli.run("refresh", "--state", state, "--write")
    assert "missing" in result.output
    assert "were kept" in result.output

    listed = cli.ok("state", "list", "--state", state, "--json")
    import json as _json

    nodes = _json.loads(listed.output)["nodes"]
    assert len(nodes) == 1, "the row survived"
    assert nodes[0]["drifted"] is True, "and is marked for re-check"


def test_refresh_write_prune_is_how_rows_are_forgotten(tmp_path: Path) -> None:
    cfg = file_config(tmp_path)
    state = tmp_path / "state.db"
    assert cli.run("apply", cfg, "--state", state, "-y").exit_code == 0
    (tmp_path / "out.txt").unlink()

    result = cli.run("refresh", "--state", state, "--write", "--prune")
    assert "were kept" not in result.output

    listed = cli.ok("state", "list", "--state", state)
    assert "state is empty" in listed.output
