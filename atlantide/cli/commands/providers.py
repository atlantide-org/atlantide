"""``atlantide providers``: which provider plugins this install can see."""

from __future__ import annotations

import typer
from rich.markup import escape
from rich.table import Table

from atlantide.cli.console import console
from atlantide.cli.context import set_json_mode
from atlantide.cli.options import JsonOpt
from atlantide.cli.views.output import emit_json
from atlantide.cli.wiring import discovery

__all__ = ["providers"]


def providers(json_out: JsonOpt = False) -> None:
    """List the provider plugins this install can see, and any that failed.

    A plugin that does not load is otherwise invisible: config cannot find its
    resource types, which looks like a typo in the config rather than a broken
    install.
    """
    set_json_mode(enabled=json_out)
    found = discovery()
    rows = [
        {
            "name": plugin.name,
            "module": plugin.module,
            "types": len(plugin.types),
            "api_version": plugin.api_version,
            "summary": plugin.summary,
        }
        for plugin in sorted(found.plugins, key=lambda p: p.name)
    ]
    # `fatal`: refused over its identity (see `PluginError`), which aborts every
    # command that builds providers, not just the runs that would have used it.
    problems = [{"name": e.name, "detail": e.detail, "fatal": e.fatal} for e in found.errors]
    if json_out:
        emit_json({"providers": rows, "errors": problems})
        # Same exit contract as the human-readable path: CI parsing the JSON
        # must see a non-zero exit when a provider failed to load.
        if problems:
            raise typer.Exit(1)
        return
    table = Table(title=f"{len(rows)} provider(s)")
    table.add_column("name", style="bold")
    table.add_column("types", justify="right")
    table.add_column("module")
    table.add_column("summary")
    for row in rows:
        table.add_row(str(row["name"]), str(row["types"]), str(row["module"]), str(row["summary"]))
    console.print(table)
    for problem in problems:
        label = "refused" if problem["fatal"] else "failed"
        console.print(
            f"[red]{label}[/] {escape(str(problem['name']))}: {escape(str(problem['detail']))}"
        )
    if problems:
        raise typer.Exit(1)
