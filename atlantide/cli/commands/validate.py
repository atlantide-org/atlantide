"""``atlantide validate``: check that a config compiles, without state or providers."""

from __future__ import annotations

from rich.markup import escape

from atlantide.cli.config_source import config_run
from atlantide.cli.console import console
from atlantide.cli.context import set_json_mode
from atlantide.cli.errors import unwrap_or_diag
from atlantide.cli.options import ConfigArg, EnvOpt, FuelOpt, JsonOpt, VarFileOpt, VarOpt
from atlantide.cli.views.output import emit_json
from atlantide.cli.wiring import stateless_engine

__all__ = ["validate"]


def validate(
    config: ConfigArg = None,
    var: VarOpt = None,
    var_file: VarFileOpt = None,
    env: EnvOpt = None,
    json_out: JsonOpt = False,
    fuel: FuelOpt = None,
) -> None:
    """Check that a config compiles: syntax, the Atlas-lang subset, and the graph.

    Touches no state and calls no provider, so it needs no credentials and cannot
    change anything. That suits a pre-commit hook or a pull-request check, where
    `plan` would need a state backend.

    Without ``--env`` every declared environment is evaluated, so a type error in
    a prod-only value is caught here rather than at the prod apply.

    It does not report what will *change*, which requires reading state; it checks
    only that the config is well-formed and its dependencies are acyclic.
    """
    set_json_mode(enabled=json_out)
    run = config_run(config, var, var_file, env)
    cfg = run.path
    with stateless_engine(run.project, fuel=fuel) as engine:
        compiled = unwrap_or_diag(
            engine.compile(run.source, str(cfg), inputs=run.inputs, envs=run.envs), run.source
        )
    if json_out:
        emit_json(
            {
                "config": str(cfg),
                "resources": len(compiled.ir.nodes),
                "envs": {
                    "declared": list(compiled.envs_declared),
                    "selected": list(compiled.envs_selected),
                },
            }
        )
        return
    summary = f"{cfg} — {len(compiled.ir.nodes)} resource(s)"
    if compiled.envs_declared:
        listed = ", ".join(compiled.envs_selected)
        summary += f", {len(compiled.envs_selected)} environment(s) [{listed}]"
    # Escaped at the boundary: rich would read the bare `[dev, prod]` as markup.
    console.print(f"[green]ok[/] {escape(summary)}, no cycles")
