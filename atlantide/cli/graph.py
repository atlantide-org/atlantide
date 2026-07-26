"""The `graph` command: render the dependency graph as a diagram.

The sole consumer of `cli.diagram`; `main` registers it on the Typer app."""

from __future__ import annotations

from typing import Annotated

import typer

from atlantide.cli.console import console
from atlantide.cli.diagram import to_dot, to_mermaid
from atlantide.cli.errors import (
    require_choice,
    unwrap_or_diag,
)
from atlantide.cli.options import (
    ConfigArg,
    VarFileOpt,
    VarOpt,
)
from atlantide.cli.wiring import (
    config_run,
    stateless_engine,
)


def graph(
    config: ConfigArg = None,
    var: VarOpt = None,
    var_file: VarFileOpt = None,
    fmt: Annotated[str, typer.Option("--format", help="mermaid | dot")] = "mermaid",
) -> None:
    """Print the resource dependency graph (Graphviz dot or Mermaid)."""
    require_choice(fmt, ("dot", "mermaid"), "format")
    run = config_run(config, var, var_file)
    with stateless_engine(run.project) as engine:
        compiled = unwrap_or_diag(
            engine.compile(run.source, str(run.path), inputs=run.inputs), run.source
        )
        digraph = compiled.graph
        rendered = to_dot(digraph) if fmt == "dot" else to_mermaid(digraph)
        console.print(rendered, markup=False, highlight=False)
