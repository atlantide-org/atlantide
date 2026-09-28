"""The evaluation fuel budget: ``[lang] fuel`` in atlantide.toml and ``--fuel``.

Fuel is a fixed bound chosen before evaluation, so it can be raised for a large
config but never changes what a config that fits in it produces.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atlantide.cli.project import MAX_FUEL, ProjectError, load_project
from atlantide.cli.wiring import stateless_engine
from atlantide.lang import DEFAULT_FUEL
from tests.support import Cli

cli = Cli()

#: A few tens of thousands of steps and no resources: well inside the default,
#: well past a budget of 1000.
BUSY = """
total = 0
for i in range(5000):
    total = total + i
"""

RESOURCES = """
from atlantide.providers.local import File

for i in range(20):
    File(f'f{i}', path=f'f{i}.txt', content=str(i))
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "infra.py"
    cfg.write_text(BUSY)
    return cfg


def _toml(project: Path, body: str) -> None:
    (project.parent / "atlantide.toml").write_text(body)


def test_the_default_is_five_million() -> None:
    assert DEFAULT_FUEL == 5_000_000
    assert load_project(Path("/nonexistent-dir")).fuel == DEFAULT_FUEL


def test_exhaustion_names_both_ways_to_raise_it(project: Path) -> None:
    out = cli.fails("validate", project, "--fuel", "1000").output
    assert "exceeded fuel budget (1000 steps)" in out
    assert "--fuel" in out and "[lang] fuel" in out


@pytest.mark.parametrize("command", ["validate", "plan", "graph", "build"])
def test_every_evaluating_command_takes_the_flag(project: Path, command: str) -> None:
    extra = ["--state", project.parent / "s.db"] if command == "plan" else []
    if command == "build":
        extra = ["--output", project.parent / "out.atlas"]
    assert "fuel budget" in cli.fails(command, project, *extra, "--fuel", "1000").output
    cli.ok(command, project, *extra)


def test_apply_and_import_take_the_flag(project: Path) -> None:
    state = project.parent / "s.db"
    assert (
        "fuel budget"
        in cli.fails("apply", project, "--state", state, "--fuel", "1000", "-y").output
    )
    out = cli.fails("import", "--config", project, "--state", state, "--fuel", "1000").output
    assert "fuel budget" in out


def test_the_toml_sets_it(project: Path) -> None:
    _toml(project, "[lang]\nfuel = 1000\n")
    assert "fuel budget (1000 steps)" in cli.fails("validate", project).output


def test_the_flag_overrides_the_toml(project: Path) -> None:
    _toml(project, "[lang]\nfuel = 1000\n")
    cli.ok("validate", project, "--fuel", "1000000")


def test_a_profile_can_raise_it(project: Path) -> None:
    _toml(project, "[lang]\nfuel = 1000\n\n[profile.big.lang]\nfuel = 1000000\n")
    cli.fails("validate", project)
    cli.ok("--profile", "big", "validate", project)


@pytest.mark.parametrize("value", ["0", "-5", '"lots"', "true", "1.5", str(MAX_FUEL + 1)])
def test_a_bad_toml_value_is_a_config_error(tmp_path: Path, value: str) -> None:
    (tmp_path / "atlantide.toml").write_text(f"[lang]\nfuel = {value}\n")
    with pytest.raises(ProjectError, match=r"\[lang\] fuel .* must be an integer between 1 and"):
        load_project(tmp_path)


def test_a_bad_toml_value_fails_the_command_cleanly(project: Path) -> None:
    _toml(project, "[lang]\nfuel = 0\n")
    out = cli.fails("validate", project).output
    assert "[lang] fuel" in out and "Traceback" not in out


@pytest.mark.parametrize("value", ["0", str(MAX_FUEL + 1)])
def test_a_bad_flag_value_is_refused(project: Path, value: str) -> None:
    cli.fails("validate", project, "--fuel", value, code=2)


def test_the_max_is_accepted(tmp_path: Path) -> None:
    (tmp_path / "atlantide.toml").write_text(f"[lang]\nfuel = {MAX_FUEL}\n")
    assert load_project(tmp_path).fuel == MAX_FUEL


def test_fuel_does_not_change_what_a_successful_compile_produces(tmp_path: Path) -> None:
    """The budget bounds evaluation; it is not an input to the IR or its hashes."""
    project = load_project(tmp_path)
    compiled = []
    for fuel in (20_000, DEFAULT_FUEL, MAX_FUEL):
        with stateless_engine(project, fuel=fuel) as engine:
            compiled.append(engine.compile(RESOURCES, "infra.py").unwrap())
    first = compiled[0]
    for other in compiled[1:]:
        assert other.hashes == first.hashes
        assert other.ir == first.ir
