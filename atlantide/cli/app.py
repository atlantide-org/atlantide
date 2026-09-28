"""The root Typer app: global flags, and every command in ``--help`` order.

Config file, state backend (local sqlite, s3 or postgres) and secrets provider
are set in ``atlantide.toml`` (:mod:`atlantide.cli.project`), optionally under a
``--profile`` overlay.

The commands live in :mod:`atlantide.cli.commands`, one module per command or
group; this module sets their names and order. The rest of the package
is split by concern: :mod:`atlantide.cli.target` resolves the profile, project
and state destination; :mod:`atlantide.cli.config_source` locates and reads the
config a command was pointed at; :mod:`atlantide.cli.wiring` builds providers
and engines; :mod:`atlantide.cli.options` holds the option types commands share;
rendering lives in :mod:`atlantide.cli.views`, ``diagram`` and ``progress``, and
error plumbing in :mod:`atlantide.cli.errors`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, cast, override

import typer
from typer.core import TyperGroup

from atlantide.cli.commands.artifact import build, deploy, verify
from atlantide.cli.commands.component import app as component_app
from atlantide.cli.commands.graph import graph
from atlantide.cli.commands.imports import import_
from atlantide.cli.commands.init import app as init_app
from atlantide.cli.commands.introspect import app as introspect_app
from atlantide.cli.commands.outputs import app as outputs_app
from atlantide.cli.commands.providers import providers
from atlantide.cli.commands.run import apply, destroy, plan, refresh
from atlantide.cli.commands.secrets import app as secret_app
from atlantide.cli.commands.state import app as state_app
from atlantide.cli.commands.validate import validate
from atlantide.cli.console import console
from atlantide.cli.context import begin
from atlantide.cli.errors import fail_error, require_choice
from atlantide.cli.wiring import version
from atlantide.core import AtlantideError
from atlantide.core.logging import LogFormat
from atlantide.core.logging import configure as configure_logging

__all__ = ["app"]


class _Root(TyperGroup):
    """The root group: a typed error that escapes a command is still rendered.

    Most failures come back as a ``Result``, but some are raised (opening a state
    backend, taking a lock, reading a keyfile). Caught here rather than in
    :func:`~atlantide.cli.main.main` so an in-process invocation renders them the
    same way, including the ``--json`` envelope.
    """

    # `ctx` is typer's vendored click Context, which it does not export.
    @override
    def invoke(self, ctx: Any) -> Any:
        try:
            return super().invoke(ctx)
        except AtlantideError as exc:
            fail_error(exc)


app = typer.Typer(cls=_Root, add_completion=True, help="Atlantide — typed, deterministic IaC.")


def _version_callback(show: bool) -> None:  # noqa: FBT001 - Typer passes the option value positionally
    if show:
        console.print(f"atlantide {version()}")
        raise typer.Exit(0)


@app.callback()
def _main(
    ctx: typer.Context,
    _version_flag: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
    debug: Annotated[
        bool,
        typer.Option(
            "--debug",
            envvar="ATLANTIDE_DEBUG",
            help="On error, print the full traceback and cause chain.",
        ),
    ] = False,
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            "-P",
            envvar="ATLANTIDE_PROFILE",
            help="Apply the \\[profile.<name>] overlay from atlantide.toml.",
        ),
    ] = None,
    no_plugins: Annotated[
        bool,
        typer.Option(
            "--no-plugins",
            help="Ignore installed provider plugins; use only the built-in providers.",
        ),
    ] = False,
    log_level: Annotated[
        str,
        typer.Option(
            "--log-level",
            envvar="ATLANTIDE_LOG_LEVEL",
            help="debug | info | warning | error. Logs go to stderr.",
        ),
    ] = "warning",
    log_format: Annotated[
        str,
        typer.Option("--log-format", help="text | json"),
    ] = "text",
    audit_log: Annotated[
        Path | None,
        typer.Option(
            "--audit-log",
            envvar="ATLANTIDE_AUDIT_LOG",
            help="Append this run's events to a JSONL file.",
        ),
    ] = None,
) -> None:
    """Atlantide — typed, deterministic IaC."""
    # Only records the flags: this runs for every subcommand, `<cmd> --help`
    # included, so reading atlantide.toml is left to the commands that need it
    # (vendored components are mounted where config is evaluated, in wiring).
    if ctx.resilient_parsing:  # shell completion
        return
    _configure_logging(log_level, log_format)
    begin(debug=debug, profile=profile, no_plugins=no_plugins, audit_log=audit_log)


def _configure_logging(level: str, fmt: str) -> None:
    """Validate ``--log-level``/``--log-format`` and route logs to stderr accordingly."""
    require_choice(level.lower(), ("debug", "info", "warning", "error"), "--log-level")
    require_choice(fmt, ("text", "json"), "--log-format")
    configure_logging(level=level.lower(), fmt=cast("LogFormat", fmt))


# Registration order is `--help` order.
app.command()(graph)
app.command()(build)
app.command()(verify)
app.command()(deploy)
app.command("import")(import_)
app.command()(plan)
app.command()(apply)
app.command()(destroy)
app.command()(refresh)
app.command()(providers)
app.command()(validate)
app.add_typer(component_app, name="component")
app.add_typer(state_app, name="state")
app.add_typer(secret_app, name="secret")
app.registered_commands += outputs_app.registered_commands
app.registered_commands += introspect_app.registered_commands
app.registered_commands += init_app.registered_commands
