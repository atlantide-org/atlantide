"""``atlantide init`` scaffolds projects that actually work.

The load-bearing test here is :func:`test_every_template_scaffolds_a_project_that_validates`,
and specifically the fact that it runs ``validate`` with *no config argument*. That
makes it assert two things at once: the template is inside the Atlas-lang subset,
and the generated ``atlantide.toml``'s ``config`` key names a file that exists. The
second is not hypothetical — ``examples/atlantide.toml`` pointed at a config that
had not existed for some time, and stayed invisible because a nearer toml shadowed
it whenever anyone ran from the directory that mattered.

A scaffolder is also the one piece of code whose output nobody reviews before
running it, so the safety tests below are about what it refuses to do.
"""

from __future__ import annotations

import dataclasses
import json
import tomllib
from pathlib import Path

import pytest
from returns.result import Failure

from atlantide.cli.project import load_project
from atlantide.cli.templates import (
    CONFIG_FILENAME,
    GITIGNORE_MARKER,
    STATE_FILENAME,
    TEMPLATE_NAMES,
    TEMPLATES,
)
from atlantide.cli.wiring import discovered_surface
from atlantide.lang.validate import validate_source
from atlantide.secrets import SecretsConfig
from atlantide.state import StateConfig
from tests.support import Cli

cli = Cli()

TOML = "atlantide.toml"


# -- the templates are real projects ------------------------------------------


@pytest.mark.parametrize("template", TEMPLATE_NAMES)
def test_every_template_scaffolds_a_project_that_validates(
    template: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scaffold, then compile through the engine from inside the project.

    ``validate`` is given no config path on purpose: it has to find one through
    the generated toml, so this covers the wiring as well as the config.
    """
    project = tmp_path / "proj"
    cli.ok("init", project, "--template", template)
    monkeypatch.chdir(project)
    assert "no cycles" in cli.ok("validate").output


@pytest.mark.parametrize("template", TEMPLATE_NAMES)
def test_every_template_is_inside_the_atlas_lang_subset(template: str) -> None:
    """Check the source directly, so a breach reports the language's own diagnostic
    (line, column, and which construct) rather than a generic scaffolding failure."""
    result = validate_source(TEMPLATES[template].config, surface=discovered_surface())
    assert not isinstance(result, Failure), result


def test_the_minimal_scaffold_applies_and_is_then_a_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole loop, with no credentials anywhere.

    Possible only because the minimal template uses the local provider. It is
    also the clearest demonstration the engine has: the second plan reports
    everything unchanged without calling a provider at all.
    """
    project = tmp_path / "proj"
    cli.ok("init", project)
    monkeypatch.chdir(project)
    cli.ok("apply", "-y")
    assert (project / "build" / "hello.txt").read_text() == "hello from atlantide\n"
    assert "1 unchanged" in cli.ok("plan").output


def test_the_scaffolded_project_ignores_its_own_secrets_and_state(tmp_path: Path) -> None:
    """The keyfile and the state db are the two files that must never be committed."""
    cli.ok("init", tmp_path)
    ignored = (tmp_path / ".gitignore").read_text()
    assert "atlantide.key" in ignored
    assert STATE_FILENAME in ignored
    # The lockfile pins component commits and their hashes; it belongs in git.
    assert "\natlantide.lock" not in ignored


# -- the generator and the parser cannot drift apart --------------------------


def test_generated_toml_round_trips_through_the_parser(tmp_path: Path) -> None:
    cli.ok(
        "init",
        tmp_path,
        "--state",
        "s3",
        "--bucket",
        "b",
        "--key",
        "k/state.json",
        "--lock-table",
        "locks",
        "--region",
        "eu-north-1",
        "--no-validate",
    )
    loaded = load_project(tmp_path)
    assert loaded.config == CONFIG_FILENAME
    assert loaded.state_backend == StateConfig(
        backend="s3", bucket="b", key="k/state.json", lock_table="locks", region="eu-north-1"
    )


def test_generated_toml_carries_the_inputs_the_template_reads(tmp_path: Path) -> None:
    """A scaffolded project runs its first command without needing a ``-var``."""
    project = tmp_path / "my-project"
    cli.ok("init", project, "--template", "aws")
    assert load_project(project).inputs == {"name_prefix": "my-project"}


def test_the_secrets_table_is_emitted_only_when_it_says_something(tmp_path: Path) -> None:
    ssm = tmp_path / "ssm"
    cli.ok("init", ssm, "--secrets", "ssm", "--prefix", "/atlantide/prod/", "--no-validate")
    assert load_project(ssm).secrets == SecretsConfig(provider="ssm", prefix="/atlantide/prod/")
    default = tmp_path / "plain"
    cli.ok("init", default)
    assert "[secrets]" not in (default / TOML).read_text()


def test_postgres_without_a_dsn_flag_notes_where_the_dsn_comes_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keeping credentials out of the repo is the supported path, so the generated
    file should say so rather than leaving a reader to wonder what is missing."""
    monkeypatch.setenv("ATLANTIDE_STATE_DSN", "postgresql://localhost/db")
    cli.ok("init", tmp_path, "--state", "postgres", "--no-validate")
    rendered = (tmp_path / TOML).read_text()
    assert "ATLANTIDE_STATE_DSN" in rendered
    assert "postgresql://localhost/db" not in rendered
    assert load_project(tmp_path).state_backend.backend == "postgres"


#: ``[state]`` keys ``init`` writes out.
_EMITTED = {"backend", "bucket", "key", "lock_table", "dsn", "schema", "region"}
#: Keys deliberately left to hand-editing: tuning knobs and credentials whose
#: right value is a property of the deployment, not of a fresh project.
_NOT_SCAFFOLDED = {
    "kms_key_id",
    "profile",
    "endpoint",
    "lock_ttl",
    "lock_renew_interval",
    "node_timeout",
}


def test_every_state_config_field_is_scaffolded_or_deliberately_skipped() -> None:
    """A key added to :class:`StateConfig` must be decided about here.

    Without this the generator quietly becomes a second, older copy of the schema
    in ``cli/project.py`` — right up until someone scaffolds a project missing the
    key their backend now requires.
    """
    assert {f.name for f in dataclasses.fields(StateConfig)} == _EMITTED | _NOT_SCAFFOLDED


# -- safety -------------------------------------------------------------------


def test_refuses_to_overwrite_an_existing_project(tmp_path: Path) -> None:
    cli.ok("init", tmp_path)
    marker = "# edited by hand\n"
    (tmp_path / CONFIG_FILENAME).write_text(marker)
    assert "--force" in cli.fails("init", tmp_path).output
    assert (tmp_path / CONFIG_FILENAME).read_text() == marker


def test_force_overwrites(tmp_path: Path) -> None:
    cli.ok("init", tmp_path)
    (tmp_path / CONFIG_FILENAME).write_text("# edited by hand\n")
    cli.ok("init", tmp_path, "--force")
    assert "Atlas-lang" in (tmp_path / CONFIG_FILENAME).read_text()


def test_refuses_to_nest_inside_an_enclosing_project(tmp_path: Path) -> None:
    """A nested toml shadows its parent for every command run below it, and the
    walk that finds it makes that silent. The message has to name the parent."""
    cli.ok("init", tmp_path)
    output = cli.fails("init", tmp_path / "sub").output
    assert str(tmp_path.resolve()) in output.replace("\n", "")
    assert not (tmp_path / "sub").exists()


def test_every_collision_is_reported_at_once(tmp_path: Path) -> None:
    """Not first-wins: three re-runs to discover three collisions is three answers
    to a question the user only asked once."""
    (tmp_path / TOML).write_text("")
    (tmp_path / CONFIG_FILENAME).write_text("")
    output = cli.fails("init", tmp_path).output
    assert TOML in output
    assert CONFIG_FILENAME in output


def test_nothing_is_written_when_a_gate_fails(tmp_path: Path) -> None:
    """The all-or-nothing property: a refusal leaves no half-scaffolded directory."""
    target = tmp_path / "proj"
    cli.fails("init", target, "--state", "s3")
    assert not target.exists()


def test_an_existing_gitignore_is_appended_to_not_replaced(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("node_modules/\n")
    cli.ok("init", tmp_path)
    cli.ok("init", tmp_path, "--force")
    ignored = (tmp_path / ".gitignore").read_text()
    assert "node_modules/" in ignored
    assert "atlantide.key" in ignored
    # Appending twice would work and look fine; it is still wrong.
    assert ignored.count(GITIGNORE_MARKER) == 1


# -- flag validation delegates to the real validators -------------------------


def test_s3_without_a_bucket_fails_with_the_backends_own_message(tmp_path: Path) -> None:
    """Asserting the backend's literal wording is the point: it proves ``init``
    carries no second copy of ``REQUIRED_KEYS``."""
    output = cli.fails("init", tmp_path, "--state", "s3").output
    assert "requires bucket, key, lock_table" in output.replace("\n", "")


def test_unknown_template_lists_the_available_ones(tmp_path: Path) -> None:
    output = cli.fails("init", tmp_path, "--template", "nope").output
    for name in TEMPLATE_NAMES:
        assert name in output


def test_unknown_state_backend_is_refused(tmp_path: Path) -> None:
    assert "--state" in cli.fails("init", tmp_path, "--state", "nope").output


def test_unknown_secrets_provider_is_refused(tmp_path: Path) -> None:
    assert "--secrets" in cli.fails("init", tmp_path, "--secrets", "nope").output


# -- json ---------------------------------------------------------------------


def test_json_output_lists_what_was_created(tmp_path: Path) -> None:
    result = cli.ok("init", tmp_path, "--json")
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["template"] == "minimal"
    assert sorted(payload["created"]) == sorted([".gitignore", TOML, CONFIG_FILENAME])


def test_json_failure_is_still_a_document(tmp_path: Path) -> None:
    """The failing case is the one a CI parser must be able to read."""
    (tmp_path / TOML).write_text("")
    result = cli.fails("init", tmp_path, "--json")
    assert json.loads(result.output)["ok"] is False


# -- the toml is toml ---------------------------------------------------------


@pytest.mark.parametrize("state", ["local", "s3", "postgres"])
@pytest.mark.parametrize("secrets", ["keyfile", "env", "ssm"])
def test_every_backend_combination_renders_parseable_toml(
    state: str, secrets: str, tmp_path: Path
) -> None:
    target = tmp_path / f"{state}-{secrets}"
    cli.ok(
        "init",
        target,
        "--state",
        state,
        "--secrets",
        secrets,
        # Enough to satisfy each backend's own validate(); irrelevant to parsing.
        *("--bucket", "b", "--key", "k", "--lock-table", "t") if state == "s3" else (),
        *("--dsn", "postgresql://localhost/db") if state == "postgres" else (),
        "--no-validate",
    )
    tomllib.loads((target / TOML).read_text())


def test_a_non_ascii_setting_survives_into_a_loadable_toml(tmp_path: Path) -> None:
    """The generated file has to be TOML, not JSON-that-looks-like-TOML.

    `json.dumps` escapes astral characters as surrogate pairs, which TOML forbids
    — so a bucket name with an emoji in it used to produce an `atlantide.toml`
    that `tomllib` refused on the very next command.
    """
    cli.ok(
        "init",
        tmp_path,
        "--state",
        "s3",
        "--bucket",
        "acme-😀",
        "--key",
        "k",
        "--lock-table",
        "t",
        "--no-validate",
    )
    assert tomllib.loads((tmp_path / TOML).read_text())["state"]["bucket"] == "acme-😀"
    assert load_project(tmp_path).state_backend.bucket == "acme-😀"


def test_a_quote_in_a_setting_cannot_inject_toml(tmp_path: Path) -> None:
    """An unescaped quote would close the string early and let the rest of the
    value become structure — a `[state]` table pointing at another backend."""
    hostile = 'b"\n[state]\nbackend = "local'
    cli.ok(
        "init",
        tmp_path,
        "--state",
        "s3",
        "--bucket",
        hostile,
        "--key",
        "k",
        "--lock-table",
        "t",
        "--no-validate",
    )
    assert load_project(tmp_path).state_backend.backend == "s3"
    assert load_project(tmp_path).state_backend.bucket == hostile


def test_gitignore_pattern_lines_carry_no_inline_comments(tmp_path: Path) -> None:
    # git treats `#` as a comment only at line start; a trailing annotation
    # would turn the whole line into a literal, never-matching pattern.
    cli.ok("init", tmp_path)
    lines = (tmp_path / ".gitignore").read_text().splitlines()
    patterns = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
    assert patterns
    for line in patterns:
        assert "#" not in line and line == line.strip()
