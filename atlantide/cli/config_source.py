"""The config a command was pointed at: located, read, and paired with its inputs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from atlantide.cli.errors import fail
from atlantide.cli.options import resolve_inputs
from atlantide.cli.project import ProjectConfig
from atlantide.cli.target import current_project

__all__ = ["ConfigRun", "config_run", "read_config", "resolve_config"]


@dataclass(frozen=True, slots=True)
class ConfigRun:
    """A config located, read, and paired with the inputs it will be given.

    The parts belong together: a path without its source may not exist, and inputs
    are meaningful only against the project whose ``[inputs]`` table they were
    merged over.
    """

    project: ProjectConfig
    path: Path
    source: str
    inputs: dict[str, Any]
    #: The environments ``--env`` named, or ``None`` for every declared one.
    #: ``None`` differs from ``()``, which would mean "act on nothing".
    envs: tuple[str, ...] | None = None


def config_run(
    config: Path | None,
    var: list[str] | None,
    var_file: list[Path] | None,
    env: list[str] | None = None,
) -> ConfigRun:
    """Resolve, read and parameterise the config a command was pointed at.

    The source is read here, not inside the engine block, so a mistyped path
    fails before a state backend is opened and a lock taken.
    """
    project = current_project()
    path = resolve_config(config, project)
    return ConfigRun(
        project=project,
        path=path,
        source=read_config(path),
        inputs=resolve_inputs(project.inputs, var_file, var),
        envs=tuple(env) if env else None,
    )


def resolve_config(config: Path | None, project: ProjectConfig) -> Path:
    """The config to evaluate.

    A path from the toml is relative to the project root; one given on the command
    line is relative to the working directory.
    """
    if config is not None:
        return config
    if project.config:
        return project.resolve(project.config)
    fail("no config given and none set in atlantide.toml (expected a .py path)")


def read_config(cfg: Path) -> str:
    """The config's source, or a diagnostic naming the path.

    A mistyped path yields a diagnostic rather than a traceback, which would also
    break ``--json`` output.
    """
    try:
        return cfg.read_text()
    except OSError as exc:
        fail(f"cannot read config {cfg}: {exc.strerror or exc}")
