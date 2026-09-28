"""The resource-facing commands: ``plan``, ``apply``, ``destroy``, ``refresh``.

Each reads or writes state, so each announces which state it targets: with a
shared backend, "no changes" and "wrong target" otherwise look the same.
"""

from __future__ import annotations

import time
from typing import Annotated, cast

import typer
from returns.result import Result

from atlantide.cli.audit import audit_file, audit_header, logging_sink
from atlantide.cli.config_source import ConfigRun, config_run
from atlantide.cli.console import console
from atlantide.cli.context import current, set_json_mode
from atlantide.cli.errors import fail, require_choice, run_async, unwrap_or_diag, unwrap_or_exit
from atlantide.cli.options import (
    ON_FAILURE_CHOICES,
    ConfigArg,
    ConfirmOpt,
    EnvOpt,
    FuelOpt,
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
from atlantide.cli.progress import maybe_live
from atlantide.cli.target import StateTarget, current_project, resolve_target
from atlantide.cli.views.drift import drift_json, render_drift
from atlantide.cli.views.output import emit_json, emit_or_render
from atlantide.cli.views.plan import plan_json, render_destroy_preview, render_plan
from atlantide.cli.views.report import render_report, report_json
from atlantide.cli.wiring import engine_for
from atlantide.core import AtlantideError
from atlantide.core.actions import Action
from atlantide.core.events import EventSink, fanout
from atlantide.engine import Engine, Plan
from atlantide.reconcile import ApplyReport, ChangeSet, DriftReport, OnFailure, ProgressCallback
from atlantide.reconcile.progress import progress_sink

__all__ = ["apply", "destroy", "plan", "refresh"]

#: Plan and apply show the same plan, so they fold NOOP rows the same way.
ShowUnchangedOpt = Annotated[
    bool,
    typer.Option(
        "--show-unchanged",
        help="List unchanged resources in the plan instead of one count per stack.",
    ),
]
DetailedExitcodePlanOpt = Annotated[
    bool,
    typer.Option(
        "--detailed-exitcode",
        help="Exit 0 (no changes), 2 (changes pending), 1 (error/denied).",
    ),
]
DryRunOpt = Annotated[bool, typer.Option("--dry-run", help="Show the plan without making changes.")]
OnFailureOpt = Annotated[
    str,
    typer.Option(
        "--on-failure",
        help="On a provider error: 'rollback' (undo completed nodes, saga; "
        "default) or 'halt' (leave completed nodes in place).",
    ),
]
AllowPlanDriftOpt = Annotated[
    bool,
    typer.Option(
        "--allow-plan-drift",
        help="Apply even if state changed since the plan shown was computed.",
    ),
]
WriteOpt = Annotated[
    bool,
    typer.Option("--write", help="Sync detected drift back into state (default: report only)."),
]
PruneOpt = Annotated[
    bool,
    typer.Option(
        "--prune",
        help="With --write, also forget resources the provider could not find.",
    ),
]
DetailedExitcodeRefreshOpt = Annotated[
    bool,
    typer.Option(
        "--detailed-exitcode",
        help="Exit 0 (no drift), 2 (drift found), 1 (error).",
    ),
]
VerboseOpt = Annotated[
    bool,
    typer.Option(
        "--verbose",
        "-v",
        help="Name the fields each provider's read did not check.",
    ),
]


def _planned(
    engine: Engine,
    run: ConfigRun,
    only: list[str] | None,
    replace: list[str] | None,
) -> Plan:
    """The plan that ``plan`` renders and ``apply`` executes."""
    return unwrap_or_diag(
        engine.plan(
            run.source,
            str(run.path),
            inputs=run.inputs,
            envs=run.envs,
            targets=only or (),
            replace=replace or (),
        ),
        run.source,
    )


def plan(  # noqa: PLR0913 - Typer command: one parameter per CLI option
    config: ConfigArg = None,
    var: VarOpt = None,
    var_file: VarFileOpt = None,
    env: EnvOpt = None,
    only: TargetOpt = None,
    replace: ReplaceOpt = None,
    state: StateOpt = None,
    json_out: JsonOpt = False,
    detailed_exitcode: DetailedExitcodePlanOpt = False,
    show_unchanged: ShowUnchangedOpt = False,
    fuel: FuelOpt = None,
) -> None:
    """Show the changes a config would make against current state.

    Exits non-zero when a mandatory policy denies the plan. With
    --detailed-exitcode, also exits 2 when changes are pending.
    """
    set_json_mode(enabled=json_out)
    run = config_run(config, var, var_file, env)
    target = resolve_target(state, run.project, announce=not json_out)
    with engine_for(target, fuel=fuel) as engine:
        plan_obj = _planned(engine, run, only, replace)
        emit_or_render(
            json_out=json_out,
            payload=lambda: plan_json(plan_obj),
            render=lambda: render_plan(
                plan_obj, targeted=bool(only or replace), show_unchanged=show_unchanged
            ),
            state=target.label,
        )
        if plan_obj.blocked:
            raise typer.Exit(1)
        # `pending`, not `actionable`: a state-only change needs an apply too.
        if detailed_exitcode and plan_obj.changeset.pending:
            raise typer.Exit(2)


def apply(  # noqa: PLR0913 - Typer command: one parameter per CLI option
    config: ConfigArg = None,
    var: VarOpt = None,
    var_file: VarFileOpt = None,
    env: EnvOpt = None,
    only: TargetOpt = None,
    replace: ReplaceOpt = None,
    state: StateOpt = None,
    dry_run: DryRunOpt = False,
    confirm: ConfirmOpt = False,
    json_out: JsonOpt = False,
    region: RegionOpt = None,
    parallelism: ParallelismOpt = None,
    on_failure: OnFailureOpt = "rollback",
    allow_plan_drift: AllowPlanDriftOpt = False,
    show_unchanged: ShowUnchangedOpt = False,
    fuel: FuelOpt = None,
) -> None:
    """Apply a config: create/update/replace/delete resources to match it.

    Shows the plan and asks for confirmation before applying; pass --confirm/-y
    (or --dry-run) to skip the prompt.

    The apply re-diffs once it holds the state lock, so what runs can differ from
    what was shown if another run landed in between. Such a difference is an
    error; --allow-plan-drift opts out.
    """
    set_json_mode(enabled=json_out)
    require_choice(on_failure, ON_FAILURE_CHOICES, "--on-failure")
    run = config_run(config, var, var_file, env)
    target = resolve_target(state, run.project, announce=not json_out)
    with engine_for(target, region=region, parallelism=parallelism, fuel=fuel) as engine:
        plan_obj = _planned(engine, run, only, replace)
        stop = _render_plan_or_stop(
            plan_obj,
            json_out=json_out,
            dry_run=dry_run,
            targeted=bool(only or replace),
            show_unchanged=show_unchanged,
            state=target.label,
        )
        if plan_obj.blocked:
            # As `plan` does: a denied plan is a failure, dry run or not.
            raise typer.Exit(1)
        if stop:
            if not dry_run:
                _audit_noop(run, target, plan_obj)
            return
        if json_out and not confirm:
            # A prompt would land on stdout inside the JSON document, after a plan
            # the operator was never shown.
            fail("--json cannot prompt for confirmation: pass --confirm/-y to apply")
        require_confirm("\nApply these changes?", confirm=confirm)
        used_live = console.is_terminal and not json_out
        started = time.perf_counter()
        result = _execute(
            engine,
            plan_obj,
            run,
            target,
            only=only,
            replace=replace,
            on_failure=cast("OnFailure", on_failure),
            # The changeset shown and approved; the engine refuses to execute a
            # different one.
            expect=None if allow_plan_drift else plan_obj.changeset,
            live=used_live,
        )
        report = unwrap_or_diag(result, run.source)
        emit_or_render(
            json_out=json_out,
            payload=lambda: {**report_json(report), "applied": True},
            render=lambda: render_report(
                report, elapsed=time.perf_counter() - started, show_nodes=not used_live
            ),
            state=target.label,
        )


def _render_plan_or_stop(
    plan_obj: Plan,
    *,
    json_out: bool,
    dry_run: bool,
    targeted: bool,
    show_unchanged: bool,
    state: str,
) -> bool:
    """Show the plan apply is about to act on.

    Returns ``True`` when apply ends here: a dry run, a denied plan, or nothing to
    change. Under ``--json`` the plan document is the output only then; otherwise
    the apply report is.
    """
    stop = dry_run or bool(plan_obj.blocked) or not plan_obj.changeset.pending
    if json_out:
        if stop:
            emit_json({**plan_json(plan_obj), "dry_run": dry_run, "applied": False, "state": state})
        return stop
    render_plan(plan_obj, targeted=targeted, show_unchanged=show_unchanged)
    if dry_run:
        console.print("[dim](dry run — no changes made)[/]")
    elif not plan_obj.changeset.pending and not plan_obj.blocked:
        console.print("[dim]nothing to apply[/]")
    return stop


def _audit_noop(run: ConfigRun, target: StateTarget, plan_obj: Plan) -> None:
    """Record a no-op apply, so the audit trail still shows who ran it and when."""
    header = audit_header("apply", run.path, target, plan_obj, 0)
    with audit_file(current().audit_log, header=header):
        pass


def _event_sinks(audit: EventSink, progress: ProgressCallback | None) -> list[EventSink]:
    """Everything that consumes the apply's event stream.

    The terminal display and the audit file consume one stream, so a phase added
    to the executor reaches both or neither.
    """
    sinks = [logging_sink, audit]
    if progress is not None:
        sinks.append(progress_sink(progress))
    return sinks


def _execute(  # noqa: PLR0913 - the apply command's resolved options, passed once
    engine: Engine,
    plan_obj: Plan,
    run: ConfigRun,
    target: StateTarget,
    *,
    only: list[str] | None,
    replace: list[str] | None,
    on_failure: OnFailure,
    expect: ChangeSet | None,
    live: bool,
) -> Result[ApplyReport, AtlantideError]:
    """Run the approved plan, with the live table and the audit file attached."""
    actionable = [(c.node_id, c.action) for c in plan_obj.changeset.pending]
    header = audit_header("apply", run.path, target, plan_obj, len(actionable))
    with (
        audit_file(current().audit_log, header=header) as audit,
        maybe_live(actionable, enabled=live) as progress,
    ):
        engine.events = fanout(*_event_sinks(audit, progress))
        # Reuse the plan's compilation: re-evaluating the config would repeat its
        # cost and could disagree with what was shown.
        return run_async(
            engine.apply_compiled(
                plan_obj.compiled,
                targets=only or (),
                replace=replace or (),
                on_failure=on_failure,
                expect=expect,
            ),
            parallelism=engine.parallelism,
        )


def destroy(
    only: TargetOpt = None,
    env: EnvOpt = None,
    state: StateOpt = None,
    confirm: ConfirmOpt = False,
    region: RegionOpt = None,
    parallelism: ParallelismOpt = None,
) -> None:
    """Destroy every resource recorded in state (shows what, then prompts).

    ``--env`` selects by stack rather than through a config: destroy reads no
    config, only state. An environment's stack is its name, so ``--env dev``
    expands to ``--target 'dev:*'``, with the same closure over dependents.
    """
    project = current_project()
    only = [*(only or ()), *(f"{name}:*" for name in env or ())] or None
    target = resolve_target(state, project)
    with engine_for(target, region=region, parallelism=parallelism) as engine:
        node_ids = unwrap_or_exit(engine.destroy_targets(only or ()))
        if not node_ids:
            console.print("[dim]nothing in state to destroy[/]")
            return
        render_destroy_preview(node_ids)
        if only:
            total = len(engine.backend.load().nodes)
            console.print(
                f"[yellow]targeting {len(node_ids)} of {total} resource(s)[/] — "
                f"the rest are not shown and will not be destroyed"
            )
        require_confirm(f"\nDestroy these {len(node_ids)} resource(s)?", confirm=confirm)
        started = time.perf_counter()
        rows = [(node_id, Action.DELETE) for node_id in node_ids]
        with maybe_live(rows, enabled=console.is_terminal) as progress:
            result = run_async(
                engine.destroy(progress=progress, targets=only or ()),
                parallelism=engine.parallelism,
            )
        report = unwrap_or_exit(result)
        render_report(
            report,
            elapsed=time.perf_counter() - started,
            title="Destroyed",
            summary=f"{len(report.deleted)} resource(s)",
            show_nodes=not console.is_terminal,
        )


def refresh(  # noqa: PLR0913 - Typer command: one parameter per CLI option
    state: StateOpt = None,
    write: WriteOpt = False,
    prune: PruneOpt = False,
    json_out: JsonOpt = False,
    region: RegionOpt = None,
    parallelism: ParallelismOpt = None,
    detailed_exitcode: DetailedExitcodeRefreshOpt = False,
    verbose: VerboseOpt = False,
) -> None:
    """Read live provider state and report drift vs. recorded state.

    Read-only unless --write is given, in which case drifted outputs are synced
    back into state.

    A resource the provider could not find is reported but *kept*, unless --prune
    is also given: a single failed read does not prove the resource is gone, and
    without its record the next apply creates a duplicate.

    Drift can only be seen in fields a provider's `read` reports; the report says
    how many of each resource's inputs that covered, and -v names the rest.
    """
    set_json_mode(enabled=json_out)
    project = current_project()
    target = resolve_target(state, project, announce=not json_out)
    with engine_for(target, region=region, parallelism=parallelism) as engine:
        if not engine.backend.load().nodes:
            if json_out:
                emit_json({**drift_json(DriftReport()), "state": target.label})
            else:
                console.print("[dim]nothing in state to refresh[/]")
            return
        report = unwrap_or_exit(
            run_async(engine.refresh(write=write, prune=prune), parallelism=engine.parallelism)
        )
        emit_or_render(
            json_out=json_out,
            payload=lambda: drift_json(report),
            render=lambda: render_drift(report, wrote=write, verbose=verbose, pruned=prune),
            state=target.label,
        )
        if detailed_exitcode and report.has_drift:
            raise typer.Exit(2)
