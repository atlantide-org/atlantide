"""Turning ``atlantide.toml`` and a resolved state target into providers and engines.

Every command starts the same way: find the project, resolve config and state,
build providers, build an engine. Config resolution lives in
:mod:`atlantide.cli.config_source` and state in :mod:`atlantide.cli.target`; this
module wires what they resolved into an :class:`~atlantide.engine.Engine`.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Any

from returns.result import Failure
from rich.markup import escape

from atlantide.cli.console import out
from atlantide.cli.context import current
from atlantide.cli.errors import fail, fail_error
from atlantide.cli.project import ProjectConfig
from atlantide.cli.target import StateTarget
from atlantide.components import mount as mount_components
from atlantide.core import ComponentError, ProviderRegistry
from atlantide.core.errors import RegistryError
from atlantide.core.plugin import Discovery
from atlantide.engine import Engine
from atlantide.graph.schedule import DEFAULT_PARALLELISM
from atlantide.lang import LanguageSurface
from atlantide.providers.loader import discover
from atlantide.reconcile.env import DEFAULT_NODE_TIMEOUT
from atlantide.state import MemoryStateBackend
from atlantide.util.errors import attach_also_failed


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

    AWS's keys are spelled at the top level of ``atlantide.toml``
    (``aws_region``, ``aws_profile``, ...); they are gathered into the provider's
    table here, so the plugin factory sees the same shape as every other plugin.
    """
    aws: dict[str, Any] = {
        "parallelism": parallelism or project.parallelism or DEFAULT_PARALLELISM,
        "region": region or project.aws_region,
        "profile": project.aws_profile,
        "endpoint": project.aws_endpoint,
        "aliases": project.aws_aliases,
    }
    # The local provider resolves relative paths against the project root and
    # confines them to it; without a project file there is no root to hand it.
    local: dict[str, Any] = dict(project.provider_tables.get("local", {}))
    local.pop("root", None)  # the root is the project's, not a user setting
    if project.root is not None:
        local["root"] = str(project.root)
    return {
        "aws": {key: value for key, value in aws.items() if value is not None},
        "local": local,
    }


def surface(found: Discovery) -> LanguageSurface:
    """What config may import, given what is installed.

    Registering a provider is not enough: config must also be able to import the
    modules its resource types live in.
    """
    return LanguageSurface(extra=frozenset(found.modules()))


def discovered_surface() -> LanguageSurface:
    return surface(discovery())


def build_providers(
    project: ProjectConfig, region: str | None = None, parallelism: int | None = None
) -> tuple[ProviderRegistry, dict[str, Any]]:
    """Build the provider registry from the discovered plugins.

    ``parallelism`` reaches the AWS plugin's factory because its client pool must
    match the scheduler's concurrency; a smaller pool serialises the apply.
    """
    found = discovery()
    _refuse_contested(found)
    settings = provider_settings(project, region, parallelism)
    registry = ProviderRegistry()
    for plugin in found.plugins:
        try:
            provider = plugin.factory(settings.get(plugin.name, {}))
        except Exception as exc:
            fail(f"provider {plugin.name!r} could not be configured: {exc}")
        # Checked here rather than by the registry: a provider built under another
        # plugin's name would register cleanly when that plugin is not installed,
        # and resources naming that provider would be routed to the wrong code.
        misnamed = plugin.provider_error(provider)
        if misnamed is not None:
            fail_error(_unregistered(plugin.name, misnamed))
        registered = registry.register(provider)
        if isinstance(registered, Failure):
            fail_error(_unregistered(plugin.name, str(registered.failure())))
    for problem in found.errors:
        # A plugin that failed to load is reported, not fatal: the run may not need
        # it, and diagnostic commands must keep working.
        out().print(
            f"[yellow]warning[/] provider plugin {problem.name!r} was not loaded: "
            f"{escape(problem.detail)}"
        )
    return registry, found.types()


def _refuse_contested(found: Discovery) -> None:
    """Abort when discovery refused a plugin over its identity.

    Such a plugin is not in ``found.plugins`` but claimed a name that is not
    unambiguously its own, so resources of that name may belong to it. Every
    refusal is reported: the first as the error, the rest attached to it.
    """
    refused = [
        _unregistered(problem.name, problem.detail) for problem in found.errors if problem.fatal
    ]
    if refused:
        first, *rest = refused
        attach_also_failed(first, rest)
        fail_error(first)


def _unregistered(plugin: str, detail: str) -> RegistryError:
    """Why ``plugin``'s provider was refused, as the error a command aborts with.

    Fatal, unlike a plugin that failed to load. A plugin refused at registration
    did load, so config can import its resource types and would compile without
    its provider: the result is an "unknown provider" error far from the cause, or
    a plan that omits the provider owning resources in state. A plugin refused over
    its identity may claim resources it does not own.
    """
    return RegistryError(f"provider plugin {plugin!r} could not be registered: {detail}")


# -- engines ------------------------------------------------------------------


def engine_for(
    state_target: StateTarget,
    *,
    region: str | None = None,
    parallelism: int | None = None,
    fuel: int | None = None,
) -> Engine:
    """The engine for a state-touching command, wired to ``state_target``.

    ``fuel`` is ``--fuel``; ``None`` falls back to the project's ``[lang] fuel``.

    The backend is opened last, so a failure building anything else cannot leave
    it open.
    """
    project = state_target.project
    _mount(project)
    registry, types = build_providers(project, region, parallelism)
    secrets = state_target.secrets()
    lang = discovered_surface()
    backend = state_target.open()
    try:
        return Engine(
            registry,
            backend,
            types,
            secrets=secrets,
            parallelism=parallelism or project.parallelism,
            lock_policy=state_target.lock_policy,
            node_timeout=project.state_backend.node_timeout or DEFAULT_NODE_TIMEOUT,
            surface=lang,
            fuel=fuel if fuel is not None else project.fuel,
        )
    except BaseException:
        backend.close()
        raise


def stateless_engine(project: ProjectConfig, *, fuel: int | None = None) -> Engine:
    """Engine for compile-only commands (graph/build); touches no state or keyfile."""
    _mount(project)
    registry, types = build_providers(project)
    return Engine(
        registry,
        MemoryStateBackend(),
        types,
        surface=discovered_surface(),
        fuel=fuel if fuel is not None else project.fuel,
    )


def _mount(project: ProjectConfig) -> None:
    """Make vendored published components importable as ``atlantide.components.*``.

    Done here, before any config is evaluated, rather than for every command: each
    vendored tree is re-hashed against atlantide.lock, and a tampered one must
    fail the commands that would import it, not ``state unlock`` or ``--help``.
    A no-op until ``atlantide component vendor`` has run.
    """
    try:
        mount_components(project.directory)
    except ComponentError as exc:
        fail_error(exc)
