"""The `import` command: adopt existing infrastructure into state."""

from __future__ import annotations

from typing import Annotated

import typer

from atlantide.cli.config_source import config_run
from atlantide.cli.context import set_json_mode
from atlantide.cli.errors import (
    fail,
    fail_error,
    run_async,
    unwrap_or_diag,
    unwrap_or_exit,
)
from atlantide.cli.options import (
    ConfigOpt,
    EnvOpt,
    FuelOpt,
    JsonOpt,
    RegionOpt,
    StateOpt,
    VarFileOpt,
    VarOpt,
)
from atlantide.cli.target import resolve_target
from atlantide.cli.views.imports import (
    import_json,
    importable_json,
    render_import,
    render_importable,
)
from atlantide.cli.views.output import emit_json, emit_or_render
from atlantide.cli.wiring import engine_for
from atlantide.core import AtlantideError
from atlantide.engine import Compiled, Engine
from atlantide.graph.select import match_targets
from atlantide.reconcile import ImportRequest
from atlantide.reconcile.adopt import ImportOutcome

__all__ = ["import_"]

NodeIdArg = Annotated[
    str | None,
    typer.Argument(help="Node to adopt (id, short form, or glob). Omit to list."),
]
ExternalIdArg = Annotated[
    str | None,
    typer.Argument(help="Provider id, for types located by one rather than by name."),
]
IdFieldOpt = Annotated[
    str | None,
    typer.Option("--id-field", help="Computed field the id is restored onto."),
]
DryRunOpt = Annotated[
    bool, typer.Option("--dry-run", help="Read and report; write nothing, take no lock.")
]
ForceOpt = Annotated[bool, typer.Option("--force", help="Overwrite an existing state row.")]
AllowDriftOpt = Annotated[
    bool,
    typer.Option("--allow-drift", help="Adopt even when the live resource differs."),
]
VerboseOpt = Annotated[
    bool, typer.Option("--verbose", "-v", help="Name the fields the read did not check.")
]


def import_(  # noqa: PLR0913 - Typer command: one parameter per CLI option
    node_id: NodeIdArg = None,
    external_id: ExternalIdArg = None,
    config: ConfigOpt = None,
    var: VarOpt = None,
    var_file: VarFileOpt = None,
    env: EnvOpt = None,
    state: StateOpt = None,
    id_field: IdFieldOpt = None,
    dry_run: DryRunOpt = False,
    force: ForceOpt = False,
    allow_drift: AllowDriftOpt = False,
    json_out: JsonOpt = False,
    region: RegionOpt = None,
    verbose: VerboseOpt = False,
    fuel: FuelOpt = None,
) -> None:
    """Bring an existing resource under management, without creating anything.

    Declare the resource in config as usual, then name its node here. The live
    resource is read through its provider, checked against what config declares,
    and recorded, so the next `plan` reports it unchanged rather than proposing
    to build a second copy.

    Some types are found by name and need nothing further; others are located by
    an id AWS assigned (a VPC's vpc_id, a certificate's arn), which config cannot
    know. Run `atlantide import` with no arguments to see which is which.

    A resource whose live values differ from config is *not* imported, since the
    next apply would change it. Reconcile the config, or pass
    --allow-drift to adopt it and let the next plan show the update.

    Nothing here creates, updates or deletes anything. The undo is
    `atlantide state rm <node>`, which forgets a row and leaves the resource.
    """
    set_json_mode(enabled=json_out)
    run = config_run(config, var, var_file, env)
    target = resolve_target(state, run.project, announce=not json_out)
    with engine_for(target, region=region, fuel=fuel) as engine:
        compiled = unwrap_or_diag(
            engine.compile(run.source, str(run.path), inputs=run.inputs, envs=run.envs),
            run.source,
        )
        if node_id is None:
            _list_importable(engine, compiled, json_out=json_out)
            return
        requests = [
            ImportRequest(nid, external_id=external_id, id_field=id_field)
            for nid in _import_selection(compiled, node_id, external_id)
        ]
        outcomes = _adopt(
            engine, compiled, requests, write=not dry_run, allow_drift=allow_drift, force=force
        )
    emit_or_render(
        json_out=json_out,
        payload=lambda: import_json(outcomes),
        render=lambda: render_import(outcomes, wrote=not dry_run, verbose=verbose),
        state=target.label,
    )
    if any(outcome.unresolved for outcome in outcomes):
        raise typer.Exit(1)


def _import_selection(compiled: Compiled, pattern: str, external_id: str | None) -> list[str]:
    """Resolve the pattern to node ids, refusing a shared id across several nodes."""
    try:
        selected = sorted(match_targets([pattern], compiled.graph.node_ids))
    except AtlantideError as exc:
        fail_error(exc)
    if external_id and len(selected) > 1:
        fail(
            f"{pattern!r} matches {len(selected)} resources, but an id names exactly "
            f"one — import them one at a time"
        )
    return selected


def _adopt(
    engine: Engine,
    compiled: Compiled,
    requests: list[ImportRequest],
    *,
    write: bool,
    allow_drift: bool,
    force: bool,
) -> list[ImportOutcome]:
    """Read, check and (unless ``write`` is off) record each requested node."""
    return unwrap_or_exit(
        run_async(
            engine.import_nodes(
                compiled, requests, write=write, allow_drift=allow_drift, force=force
            )
        )
    )


def _list_importable(engine: Engine, compiled: Compiled, *, json_out: bool) -> None:
    """What this config declares that state does not track, and what each needs.

    The same set a plan reports as CREATE: each is either a resource that does not
    exist yet, or an existing one that can be adopted.
    """
    node_ids = engine.importable(compiled)
    types = {node.id: node.type for node in compiled.ir.nodes}
    identity = engine.identity_fields(compiled, node_ids)
    if json_out:
        emit_json(importable_json(node_ids, types, identity))
        return
    render_importable(node_ids, identity)
