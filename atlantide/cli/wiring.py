"""Turning flags and ``atlantide.toml`` into the objects a command runs against.

Every command starts the same way — find the project, resolve which config and
which state, build providers, build an engine — and none of that is what the
command is *about*. Keeping it here leaves each command body to the thing it
actually does, and means there is one place to look when the answer to "which
state did that run touch?" is not the expected one.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

from rich.markup import escape

from atlantide.cli.console import console
from atlantide.cli.context import current, set_json_mode
from atlantide.cli.errors import fail
from atlantide.cli.options import resolve_inputs
from atlantide.cli.project import ProjectConfig
from atlantide.cli.target import StateTarget, load_project
from atlantide.core import ProviderRegistry
from atlantide.core.plugin import Discovery
from atlantide.engine import Engine, Plan
from atlantide.graph.schedule import DEFAULT_PARALLELISM
from atlantide.lang import LanguageSurface
from atlantide.providers.loader import discover
from atlantide.reconcile.context import DEFAULT_NODE_TIMEOUT
from atlantide.state import MemoryStateBackend


def version() -> str:
    try:
        return _pkg_version("atlantide")
    except PackageNotFoundError:  # running from a source tree
        from atlantide import __version__

        return __version__


# -- providers ----------------------------------------------------------------


def discovery() -> Discovery:
    """Every installed provider plugin, including the ones that ship here."""
    return discover(enabled=not current().no_plugins)


def provider_settings(
    project: ProjectConfig, region: str | None, parallelism: int | None
) -> dict[str, dict[str, Any]]:
    """Per-provider settings tables, as each plugin's factory expects them.

    AWS's keys are still spelled at the top level of ``atlantide.toml``
    (``aws_region``, ``aws_profile``, ...) because that is what existing projects
    have; they are gathered into the provider's table here rather than making
    every project rewrite its config for a mechanism it did not ask for.
    """
    aws: dict[str, Any] = {
        "parallelism": parallelism or project.parallelism or DEFAULT_PARALLELISM,
        "region": region or project.aws_region,
        "profile": project.aws_profile,
        "endpoint": project.aws_endpoint,
        "aliases": project.aws_aliases,
    }
    return {"aws": {key: value for key, value in aws.items() if value is not None}}


def surface(found: Discovery) -> LanguageSurface:
    """What config may import, given what is installed.

    A plugin's resource types are useless if config cannot name the module they
    live in — which is why the registry alone was never enough to make
    third-party providers work.
    """
    return LanguageSurface(extra=frozenset(found.modules()))


def discovered_surface() -> LanguageSurface:
    return surface(discovery())


def providers(
    project: ProjectConfig, region: str | None = None, parallelism: int | None = None
) -> tuple[ProviderRegistry, dict[str, Any]]:
    """Build the provider registry from the discovered plugins.

    ``parallelism`` reaches the AWS plugin's factory because its client pool has
    to be sized to the concurrency the scheduler will actually use — a pool too
    small silently serialises the apply.
    """
    found = discovery()
    settings = provider_settings(project, region, parallelism)
    registry = ProviderRegistry()
    for plugin in found.plugins:
        try:
            registry.register(plugin.factory(settings.get(plugin.name, {})))
        except Exception as exc:
            fail(f"provider {plugin.name!r} could not be configured: {exc}")
    for problem in found.errors:
        # A plugin that failed to load is reported, not fatal: the run may not need
        # it, and the commands used to diagnose it must keep working.
        console.print(
            f"[yellow]warning[/] provider plugin {problem.name!r} was not loaded: "
            f"{escape(problem.detail)}"
        )
    return registry, found.types()


# -- state and engines --------------------------------------------------------


def target(state: Path | None, project: ProjectConfig, *, announce: bool = True) -> StateTarget:
    """This command's state target. Announced unless the output is machine-readable,
    where the same value rides along as a ``state`` field instead."""
    resolved = StateTarget.resolve(state, project)
    if announce:
        resolved.announce()
    return resolved


def engine_for(
    state_target: StateTarget,
    *,
    region: str | None = None,
    parallelism: int | None = None,
) -> Engine:
    """The engine for a state-touching command, wired to ``state_target``."""
    project = state_target.project
    registry, types = providers(project, region, parallelism)
    return Engine(
        registry,
        state_target.open(),
        types,
        secrets=state_target.secrets(),
        parallelism=parallelism or project.parallelism,
        lock_policy=project.state_backend.lock_policy(),
        node_timeout=project.state_backend.node_timeout or DEFAULT_NODE_TIMEOUT,
        surface=discovered_surface(),
    )


def stateless_engine(project: ProjectConfig) -> Engine:
    """Engine for compile-only commands (graph/build); touches no state or keyfile."""
    registry, types = providers(project)
    return Engine(registry, MemoryStateBackend(), types, surface=discovered_surface())


def machine_readable(json_out: bool) -> None:
    """Declare that stdout belongs to a JSON document, so every human-facing
    print — banners, warnings, errors — routes to stderr instead of corrupting it."""
    set_json_mode(json_out)


def run_header(
    command: str, cfg: Path, state_target: StateTarget, plan_obj: Plan, planned: int
) -> dict[str, Any]:
    """What identifies this run in its audit record.

    The events alone are a list of things that happened to nothing in
    particular; this is what makes the file a trail.
    """
    return {
        "command": command,
        "config": str(cfg),
        "state": state_target.label,
        "version": version(),
        "inputs": plan_obj.compiled.inputs,
        "planned": planned,
    }


# -- the config a command was pointed at --------------------------------------


@dataclass(frozen=True, slots=True)
class ConfigRun:
    """A config located, read, and paired with the inputs it will be given.

    One object because the four are never useful apart: a path with no source is
    a file that may not exist, and inputs are only meaningful against the project
    whose ``[inputs]`` table they were merged over.
    """

    project: ProjectConfig
    path: Path
    source: str
    inputs: dict[str, Any]


def config_run(
    config: Path | None,
    var: list[str] | None,
    var_file: list[Path] | None,
) -> ConfigRun:
    """Resolve, read and parameterise the config a command was pointed at.

    Reading the source here rather than inside the engine block is deliberate: a
    mistyped path should fail before a state backend is opened and a lock taken,
    not after.
    """
    project = load_project()
    path = resolve_config(config, project)
    return ConfigRun(
        project=project,
        path=path,
        source=read_config(path),
        inputs=resolve_inputs(project.inputs, var_file, var),
    )


def resolve_config(config: Path | None, project: ProjectConfig) -> Path:
    """The config to evaluate. A path from the toml is relative to the project
    root; one typed on the command line is relative to where it was typed."""
    if config is not None:
        return config
    if project.config:
        return project.resolve(project.config)
    fail("no config given and none set in atlantide.toml (expected a .py path)")


def read_config(cfg: Path) -> str:
    """The config's source, or a diagnostic naming the path.

    A mistyped path is the most ordinary mistake there is, and an unguarded
    ``read_text`` answers it with a Python traceback — which under ``--json``
    is also not a document any consumer can read.
    """
    try:
        return cfg.read_text()
    except OSError as exc:
        fail(f"cannot read config {cfg}: {exc.strerror or exc}")
