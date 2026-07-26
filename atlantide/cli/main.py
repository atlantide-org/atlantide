"""atlantide command-line interface.

Commands: ``init`` (scaffold a project); ``plan`` | ``apply`` | ``destroy`` |
``refresh`` | ``graph``; ``build`` |
``verify`` | ``deploy`` (portable ``.atlas`` artifacts); ``resources`` | ``schema``;
``component`` (published components) | ``secret`` (local secrets store) |
``state check`` / ``migrate`` / ``unlock`` (backend administration).
Config file, state backend (local sqlite, s3 or postgres) and secrets provider
are set in ``atlantide.toml`` (:mod:`atlantide.cli.project`), optionally under a
``--profile`` overlay.

Every command that reads or writes state announces which state it is: with a
shared backend the difference between "no changes" and "wrong target" is
otherwise invisible until something is destroyed.

This module holds the resource-facing commands and the engine/provider wiring.
The rest is split by concern: :mod:`atlantide.cli.target` resolves the profile,
project and state destination; :mod:`atlantide.cli.state` and
:mod:`atlantide.cli.component` own their subcommand groups;
:mod:`atlantide.cli.options` the option types they share; rendering lives in
:mod:`atlantide.cli.render` / ``json_out`` / ``diagram`` / ``progress``, and
error plumbing in :mod:`atlantide.cli.errors`.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Annotated, Any, cast

import typer
from rich.markup import escape
from rich.table import Table

from atlantide.cli.artifact import build, deploy, verify
from atlantide.cli.audit import audit_file, logging_sink
from atlantide.cli.component import app as component_app
from atlantide.cli.console import console
from atlantide.cli.context import begin, current
from atlantide.cli.errors import (
    fail_error,
    require_choice,
    run_async,
    unwrap_or_diag,
    unwrap_or_exit,
)
from atlantide.cli.graph import graph as graph_cmd
from atlantide.cli.import_cmd import import_
from atlantide.cli.init import app as init_app
from atlantide.cli.introspect import app as introspect_app
from atlantide.cli.json_out import (
    drift_json,
    emit_json,
    emit_or_render,
    plan_json,
    report_json,
)
from atlantide.cli.options import (
    ON_FAILURE_CHOICES,
    ConfigArg,
    ConfirmOpt,
    JsonOpt,
    ParallelismOpt,
    RegionOpt,
    ReplaceOpt,
    StateOpt,
    TargetOpt,
    VarFileOpt,
    VarOpt,
    require_confirm,
)
from atlantide.cli.outputs import app as outputs_app
from atlantide.cli.progress import maybe_live
from atlantide.cli.render import (
    render_destroy_preview,
    render_drift,
    render_plan,
    render_report,
)
from atlantide.cli.secrets import app as secret_app
from atlantide.cli.state import app as state_app
from atlantide.cli.target import load_project
from atlantide.cli.wiring import (
    ConfigRun,
    config_run,
    discovery,
    engine_for,
    machine_readable,
    run_header,
    stateless_engine,
    version,
)
from atlantide.cli.wiring import target as state_target
from atlantide.components import mount as mount_components
from atlantide.core import ComponentError
from atlantide.core.events import fanout
from atlantide.core.logging import configure as configure_logging
from atlantide.engine import Engine, Plan
from atlantide.reconcile import Action, OnFailure
from atlantide.reconcile.context import progress_sink

app = typer.Typer(add_completion=True, help="Atlantide — typed, deterministic IaC.")

# Commands that live in their own module by topic. Registered here so `--help`
# order and command names are exactly what they were when everything sat in one
# file; the modules hold the implementations.
app.command()(graph_cmd)
app.command()(build)
app.command()(verify)
app.command()(deploy)
app.command("import")(import_)


def _version_callback(show: bool) -> None:
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
        typer.Option("--debug", help="On error, print the full traceback and cause chain."),
    ] = False,
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            "-P",
            envvar="ATLANTIDE_PROFILE",
            help="Apply the [profile.<name>] overlay from atlantide.toml.",
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
    require_choice(log_level.lower(), ("debug", "info", "warning", "error"), "--log-level")
    require_choice(log_format, ("text", "json"), "--log-format")
    configure_logging(level=log_level.lower(), fmt=cast("Any", log_format))
    begin(debug=debug, profile=profile, no_plugins=no_plugins, audit_log=audit_log)
    # Make vendored published components importable as `atlantide.components.*`
    # before a config is evaluated; a no-op until `atlantide component vendor` has
    # run. Mounting re-hashes each vendored tree against atlantide.lock, so a
    # tampered component fails here rather than at import. `component` is exempt:
    # `vendor`/`lock` are what produce a matching tree.
    try:
        mount_components(load_project().directory, verify=ctx.invoked_subcommand != "component")
    except ComponentError as exc:
        fail_error(exc)


def _planned(
    engine: Engine,
    run: ConfigRun,
    only: list[str] | None,
    replace: list[str] | None,
) -> Plan:
    """The plan `plan` renders and `apply` executes — the same call in both."""
    return unwrap_or_diag(
        engine.plan(
            run.source,
            str(run.path),
            inputs=run.inputs,
            targets=only or (),
            replace=replace or (),
        ),
        run.source,
    )


@app.command()
def plan(
    config: ConfigArg = None,
    var: VarOpt = None,
    var_file: VarFileOpt = None,
    only: TargetOpt = None,
    replace: ReplaceOpt = None,
    state: StateOpt = None,
    json_out: JsonOpt = False,
    detailed_exitcode: Annotated[
        bool,
        typer.Option(
            "--detailed-exitcode",
            help="Exit 0 (no changes), 2 (changes pending), 1 (error/denied).",
        ),
    ] = False,
) -> None:
    """Show the changes a config would make against current state.

    Exits non-zero when a mandatory policy denies the plan. With
    --detailed-exitcode, also exits 2 when changes are pending.
    """
    machine_readable(json_out)
    run = config_run(config, var, var_file)
    target = state_target(state, run.project, announce=not json_out)
    with engine_for(target) as engine:
        plan_obj = _planned(engine, run, only, replace)
        emit_or_render(
            json_out,
            payload=lambda: plan_json(plan_obj),
            render=lambda: render_plan(plan_obj, targeted=bool(only or replace)),
            state=target.label,
        )
        if plan_obj.blocked:
            raise typer.Exit(1)  # a mandatory policy denies this plan
        if detailed_exitcode and plan_obj.changeset.actionable:
            raise typer.Exit(2)  # changes pending


@app.command()
def apply(
    config: ConfigArg = None,
    var: VarOpt = None,
    var_file: VarFileOpt = None,
    only: TargetOpt = None,
    replace: ReplaceOpt = None,
    state: StateOpt = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show the plan without making changes.")
    ] = False,
    confirm: ConfirmOpt = False,
    json_out: JsonOpt = False,
    region: RegionOpt = None,
    parallelism: ParallelismOpt = None,
    on_failure: Annotated[
        str,
        typer.Option(
            "--on-failure",
            help="On a provider error: 'rollback' (undo completed nodes, saga; "
            "default) or 'halt' (leave completed nodes in place).",
        ),
    ] = "rollback",
    allow_plan_drift: Annotated[
        bool,
        typer.Option(
            "--allow-plan-drift",
            help="Apply even if state changed since the plan shown was computed.",
        ),
    ] = False,
) -> None:
    """Apply a config: create/update/replace/delete resources to match it.

    Shows the plan and asks for confirmation before applying; pass --confirm/-y
    (or --dry-run) to skip the prompt.

    The apply re-diffs once it holds the state lock, so what runs can differ from
    what was shown — another run may have landed in between. That difference is
    an error rather than a silent substitution; --allow-plan-drift opts out.
    """
    require_choice(on_failure, ON_FAILURE_CHOICES, "--on-failure")
    machine_readable(json_out)
    run = config_run(config, var, var_file)
    cfg, source, inputs = run.path, run.source, run.inputs
    target = state_target(state, run.project, announce=not json_out)
    with engine_for(target, region=region, parallelism=parallelism) as engine:
        plan_obj = _planned(engine, run, only, replace)
        if not json_out:
            render_plan(plan_obj, targeted=bool(only or replace))
        if dry_run:
            if not json_out:
                console.print("[dim](dry run — no changes made)[/]")
            return
        if not plan_obj.changeset.actionable:
            if not json_out:
                console.print("[dim]nothing to apply[/]")
            # Recorded even though nothing changed: an audit trail that omits
            # no-op runs cannot answer "who ran apply against prod, and when".
            header = run_header("apply", cfg, target, plan_obj, 0)
            with audit_file(current().audit_log, header=header):
                pass
            return
        require_confirm(confirm, "\nApply these changes?")
        # The changeset the operator just saw and approved; the engine refuses to
        # execute a different one unless told otherwise.
        expected = None if allow_plan_drift else plan_obj.changeset
        actionable = [(c.node_id, c.action) for c in plan_obj.changeset.actionable]
        used_live = console.is_terminal and not json_out
        started = time.perf_counter()
        header = run_header("apply", cfg, target, plan_obj, len(actionable))
        with (
            audit_file(current().audit_log, header=header) as audit,
            maybe_live(actionable, enabled=used_live) as progress,
        ):
            # The terminal display and the audit file consume one stream, so a
            # phase added to the executor reaches both or neither.
            sinks = [logging_sink, audit]
            if progress is not None:
                sinks.append(progress_sink(progress))
            engine.events = fanout(*sinks)
            result = run_async(
                engine.apply(
                    source,
                    str(cfg),
                    inputs=inputs,
                    targets=only or (),
                    replace=replace or (),
                    on_failure=cast(OnFailure, on_failure),
                    expect=expected,
                )
            )
        report = unwrap_or_diag(result, source)
        emit_or_render(
            json_out,
            payload=lambda: report_json(report),
            render=lambda: render_report(
                report, elapsed=time.perf_counter() - started, show_nodes=not used_live
            ),
            state=target.label,
        )


@app.command()
def destroy(
    only: TargetOpt = None,
    state: StateOpt = None,
    confirm: ConfirmOpt = False,
    region: RegionOpt = None,
    parallelism: ParallelismOpt = None,
) -> None:
    """Destroy every resource recorded in state (shows what, then prompts)."""
    project = load_project()
    with engine_for(state_target(state, project), region=region, parallelism=parallelism) as engine:
        node_ids = unwrap_or_exit(engine.destroy_targets(only or ()))
        if not node_ids:
            console.print("[dim]nothing in state to destroy[/]")
            return
        render_destroy_preview(node_ids)  # show what will be removed first
        if only:
            total = len(engine.backend.load().nodes)
            console.print(
                f"[yellow]targeting {len(node_ids)} of {total} resource(s)[/] — "
                f"the rest are not shown and will not be destroyed"
            )
        require_confirm(confirm, f"\nDestroy these {len(node_ids)} resource(s)?")
        started = time.perf_counter()
        rows = [(node_id, Action.DELETE) for node_id in node_ids]
        with maybe_live(rows, enabled=console.is_terminal) as progress:
            result = run_async(engine.destroy(progress=progress, targets=only or ()))
        report = unwrap_or_exit(result)
        render_report(
            report,
            elapsed=time.perf_counter() - started,
            title="Destroyed",
            summary=f"{len(report.deleted)} resource(s)",
            show_nodes=not console.is_terminal,
        )


@app.command()
def refresh(
    state: StateOpt = None,
    write: Annotated[
        bool,
        typer.Option("--write", help="Sync detected drift back into state (default: report only)."),
    ] = False,
    prune: Annotated[
        bool,
        typer.Option(
            "--prune",
            help="With --write, also forget resources the provider could not find.",
        ),
    ] = False,
    json_out: JsonOpt = False,
    region: RegionOpt = None,
    parallelism: ParallelismOpt = None,
    detailed_exitcode: Annotated[
        bool,
        typer.Option(
            "--detailed-exitcode",
            help="Exit 0 (no drift), 2 (drift found), 1 (error).",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Name the fields each provider's read did not check.",
        ),
    ] = False,
) -> None:
    """Read live provider state and report drift vs. recorded state.

    Read-only unless --write is given, in which case drifted outputs are synced
    back into state.

    A resource the provider could not find is reported but *kept*, unless --prune
    is also given: one failed read is not enough evidence to discard the only
    record that a resource exists, and discarding it means the next apply builds
    a second one.

    Drift can only be seen in fields a provider's `read` reports; the report says
    how many of each resource's inputs that covered, and -v names the rest.
    """
    machine_readable(json_out)
    project = load_project()
    target = state_target(state, project, announce=not json_out)
    with engine_for(target, region=region, parallelism=parallelism) as engine:
        if not engine.backend.load().nodes:
            if not json_out:
                console.print("[dim]nothing in state to refresh[/]")
            return
        report = unwrap_or_exit(run_async(engine.refresh(write=write, prune=prune)))
        emit_or_render(
            json_out,
            payload=lambda: drift_json(report),
            render=lambda: render_drift(report, wrote=write, verbose=verbose, pruned=prune),
            state=target.label,
        )
        if detailed_exitcode and report.has_drift:
            raise typer.Exit(2)


@app.command()
def providers(json_out: JsonOpt = False) -> None:
    """List the provider plugins this install can see, and any that failed.

    A plugin that does not load is otherwise invisible: config simply cannot find
    its resource types, which looks like a typo in the config rather than a
    broken install. This is where that difference becomes legible.
    """
    machine_readable(json_out)
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
    problems = [{"name": e.name, "detail": e.detail} for e in found.errors]
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
        console.print(
            f"[red]failed[/] {escape(str(problem['name']))}: {escape(str(problem['detail']))}"
        )
    if problems:
        raise typer.Exit(1)


@app.command()
def validate(
    config: ConfigArg = None,
    var: VarOpt = None,
    var_file: VarFileOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Check that a config compiles: syntax, the Atlas-lang subset, and the graph.

    Touches no state and calls no provider, so it needs no credentials and cannot
    change anything — which makes it the check to run in a pre-commit hook or on
    a pull request, where `plan` would need a backend it should not have.

    It cannot tell you what will *change* — that requires reading state — only
    that the config is well-formed and its dependencies are acyclic.
    """
    machine_readable(json_out)
    run = config_run(config, var, var_file)
    cfg = run.path
    with stateless_engine(run.project) as engine:
        compiled = unwrap_or_diag(
            engine.compile(run.source, str(cfg), inputs=run.inputs), run.source
        )
    if json_out:
        emit_json({"config": str(cfg), "resources": len(compiled.ir.nodes)})
        return
    console.print(
        f"[green]ok[/] {escape(str(cfg))} — {len(compiled.ir.nodes)} resource(s), no cycles"
    )


app.add_typer(component_app, name="component")
app.add_typer(state_app, name="state")
app.add_typer(secret_app, name="secret")
app.registered_commands += outputs_app.registered_commands
app.registered_commands += introspect_app.registered_commands
app.registered_commands += init_app.registered_commands


def main() -> None:
    app()


if __name__ == "__main__":
    main()
