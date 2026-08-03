"""Human-readable (Rich) rendering of plans, reports, and drift."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Mapping
from typing import Any

from rich.markup import escape
from rich.rule import Rule

from atlantide.cli.console import console
from atlantide.core import PolicyLevel
from atlantide.core.fields import Mutability
from atlantide.core.markers import contains_ref
from atlantide.core.node_id import group_by_stack, short_id
from atlantide.engine import Compiled, Plan
from atlantide.reconcile import Action, ApplyReport, Change, Drift, DriftReport, NodeDrift
from atlantide.reconcile.adopt import ImportOutcome, ImportStatus
from atlantide.secrets import is_secret_ref_marker

SECRET_REDACTED = "(sensitive)"

SIGN = {
    Action.CREATE: ("+", "green"),
    Action.UPDATE: ("~", "yellow"),
    Action.REPLACE: ("±", "magenta"),
    Action.DELETE: ("-", "red"),
    Action.NOOP: ("=", "dim"),
}

MUT_COLOR = {
    Mutability.MUTABLE: "yellow",
    Mutability.IMMUTABLE: "magenta",
    Mutability.COMPUTED: "cyan",
}

# create -> add, update/replace -> change, delete -> destroy (terraform-style summary).
_SUMMARY_BUCKET = {
    Action.CREATE: "add",
    Action.UPDATE: "change",
    Action.REPLACE: "change",
    Action.DELETE: "destroy",
}

_DRIFT_SIGN = {
    Drift.IN_SYNC: ("=", "dim", "in sync"),
    Drift.DRIFTED: ("~", "yellow", "drifted"),
    Drift.MISSING: ("-", "red", "missing"),
}


def summary_bar(counts: Counter[Action]) -> str:
    """A ``2 to add, 1 to change, 1 to destroy`` line (plus unchanged if any)."""
    totals: Counter[str] = Counter()
    for action, n in counts.items():
        if action is not Action.NOOP:
            totals[_SUMMARY_BUCKET[action]] += n
    parts = [f"{totals[b]} to {b}" for b in ("add", "change", "destroy") if totals[b]]
    if counts.get(Action.NOOP):
        parts.append(f"{counts[Action.NOOP]} unchanged")
    return ", ".join(parts) or "no changes"


def fmt_value(value: Any, limit: int = 60) -> str:
    """A short, human display of a property value (refs/secrets are redacted)."""
    if is_secret_ref_marker(value):
        return SECRET_REDACTED
    if contains_ref(value):
        return "(known after apply)"
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def field_diffs(change: Change) -> list[str]:
    """``field: old → new`` lines for an UPDATE/REPLACE, from prior vs desired props."""
    if change.action not in (Action.UPDATE, Action.REPLACE):
        return []
    prior = change.prior.properties if change.prior else {}
    desired = change.desired.properties if change.desired else {}
    lines = []
    for field in change.changed_fields:
        lines.append(f"{field}: {fmt_value(prior.get(field))} → {fmt_value(desired.get(field))}")
    return lines


def stack_sections(node_ids: list[str]) -> Iterator[str]:
    """Yield node ids grouped by stack, printing each stack's Rule header first."""
    for stack, ids in group_by_stack(node_ids).items():
        console.print(Rule(f"[bold]{stack}[/]", align="left", style="dim"))
        yield from ids


def render_plan(plan_obj: Plan, *, targeted: bool = False) -> None:
    render_inputs(plan_obj.compiled.inputs)
    render_envs(plan_obj.compiled)
    if targeted:
        render_targeting(plan_obj)
    changeset = plan_obj.changeset
    changes = {c.node_id: c for c in changeset.changes}
    for node_id in stack_sections([c.node_id for c in changeset.changes]):
        change = changes[node_id]
        sign, color = SIGN[change.action]
        label = f"{change.action.value:<7}"
        console.print(f"  [{color}]{sign} {label}[/] {short_id(node_id)}{_plan_suffix(change)}")
        for line in field_diffs(change):
            console.print(f"      [dim]{escape(line)}[/]")
    counts = Counter(change.action for change in changeset.changes)
    console.print(f"\n[bold]Plan:[/] {summary_bar(counts)}")
    render_declared_outputs(plan_obj.compiled.outputs)
    render_violations(plan_obj)
    render_warnings(plan_obj)


def render_targeting(plan_obj: Plan) -> None:
    """Say plainly that this plan is a subset.

    The danger of targeting is not the narrowing, it is forgetting: a plan that
    reads "no changes" because everything else was filtered out looks exactly
    like a plan that reads "no changes" because everything is up to date.
    """
    total = len(plan_obj.compiled.graph.node_ids)
    acting = len(plan_obj.changeset.actionable)
    console.print(
        f"[yellow]targeting[/] {acting} change(s) across {total} resource(s) — "
        f"anything not selected is not shown and will not change"
    )


def render_inputs(inputs: dict[str, Any]) -> None:
    """The config inputs this plan was computed from.

    Two runs of the same config can legitimately plan differently when their
    inputs differ; without showing them, "why is today's plan different" has no
    answer visible anywhere.
    """
    if not inputs:
        return
    shown = ", ".join(f"{key}={fmt_value(value)}" for key, value in sorted(inputs.items()))
    console.print(f"[dim]inputs: {escape(shown)}[/]")


def render_envs(compiled: Compiled) -> None:
    """Name the environments this plan covers, when it does not cover them all.

    Excluded environments are outside the run rather than unchanged: their
    resources are not diffed and will not be touched. Silent when nothing was
    narrowed.
    """
    if not compiled.envs_excluded:
        return
    console.print(
        f"[yellow]envs:[/] {', '.join(compiled.envs_selected)} "
        f"[dim](of {', '.join(compiled.envs_declared)})[/] — "
        f"{', '.join(compiled.envs_excluded)} is not planned and will not change"
    )


def _plan_suffix(change: Change) -> str:
    tags = []
    if change.conditional:
        tags.append("known after apply")
    if change.action is Action.REPLACE and change.create_before_destroy:
        tags.append("create before destroy")
    # ``\[`` escapes the literal bracket so rich doesn't parse it as a markup tag.
    return rf"  [dim]\[{', '.join(tags)}][/]" if tags else ""


def render_warnings(plan_obj: Plan) -> None:
    for message in plan_obj.warnings:
        console.print(f"[yellow]warning[/] {escape(message)}")


def render_declared_outputs(outputs: dict[str, Any]) -> None:
    if not outputs:
        return
    console.print("\n[bold]Outputs:[/]")
    for key, value in outputs.items():
        if is_secret_ref_marker(value):
            detail = f"[dim]{SECRET_REDACTED}[/]"
        elif contains_ref(value):
            detail = "[dim](known after apply)[/]"
        else:
            detail = escape(repr(value))
        console.print(f"  {key} = {detail}")


def render_violations(plan_obj: Plan) -> None:
    for v in plan_obj.violations:
        mandatory = v.level is PolicyLevel.MANDATORY
        color = "red" if mandatory else "yellow"
        tag = "DENY" if mandatory else "WARN"
        console.print(f"[{color}]policy {tag}[/] {v.policy}: {v.message}")
    if plan_obj.blocked:
        n = len(plan_obj.blocked)
        console.print(f"[bold red]{n} mandatory policy violation(s) block apply[/]")


def render_destroy_preview(node_ids: list[str]) -> None:
    """List what a destroy will remove, grouped by stack, before the prompt."""
    sign, color = SIGN[Action.DELETE]
    for node_id in stack_sections(node_ids):
        console.print(f"  [{color}]{sign} destroy[/] {short_id(node_id)}")
    console.print(f"\n[bold]Plan:[/] {len(node_ids)} to destroy")


def render_report(
    report: ApplyReport,
    elapsed: float | None = None,
    *,
    title: str = "Applied",
    summary: str | None = None,
    show_nodes: bool = True,
) -> None:
    # Same +/~/±/- language as the plan, grouped by stack. When a live progress
    # table already showed the per-node lines, pass show_nodes=False (summary only).
    if show_nodes:
        action_of = {
            **dict.fromkeys(report.created, Action.CREATE),
            **dict.fromkeys(report.updated, Action.UPDATE),
            **dict.fromkeys(report.replaced, Action.REPLACE),
            **dict.fromkeys(report.deleted, Action.DELETE),
        }
        for node_id in stack_sections(list(action_of)):
            sign, color = SIGN[action_of[node_id]]
            console.print(f"  [{color}]{sign} done[/] {short_id(node_id)}")
    counts = Counter(
        {
            Action.CREATE: len(report.created),
            Action.UPDATE: len(report.updated),
            Action.REPLACE: len(report.replaced),
            Action.DELETE: len(report.deleted),
            Action.NOOP: len(report.noop),
        }
    )
    took = f"  [dim]({elapsed:.1f}s)[/]" if elapsed is not None else ""
    console.print(f"\n[bold]{title}:[/] {summary or summary_bar(counts)}{took}")
    _render_trouble(report)
    if report.outputs:
        console.print("\n[bold]Outputs:[/]")
        for key, value in report.outputs.items():
            redact = is_secret_ref_marker(value) or key in report.sensitive_outputs
            shown = SECRET_REDACTED if redact else escape(str(value))
            console.print(f"  {key} = {shown}")


def _render_trouble(report: ApplyReport) -> None:
    """Everything that went wrong on the way, worst last.

    Ordered deliberately: each of these leaves state and reality further apart
    than the one before it, and the last thing printed is the thing most likely
    to still need a human.
    """
    if report.rolled_back:
        console.print(f"[yellow]rolled back {len(report.rolled_back)} node(s)[/]")
    if report.rollback_skipped:
        # Deliberate, not a failure — but the resources are still there, so say so
        # rather than leaving the operator to infer it from silence.
        console.print(
            f"[bold red]rollback skipped[/] — {escape(report.rollback_skipped)}\n"
            "[dim]resources this run created were left in place; run "
            "`atlantide refresh` to see what exists[/]"
        )
    # A half-done compensation leaves state and the provider disagreeing. The rows
    # are marked stale so the next plan re-diffs them instead of skipping them as
    # NOOP, but the result still needs checking by hand.
    _per_node(
        report.rollback_failed,
        f"[bold red]rollback incomplete for {len(report.rollback_failed)} node(s)[/] "
        "— state may not describe the live resources:",
        footer="these rows are marked stale, so the next plan will re-check them "
        "instead of reporting no change",
    )
    # The stale mark itself did not land, so the next plan *will* skip these.
    _per_node(
        report.poison_failed,
        f"[bold red]{len(report.poison_failed)} node(s) could not be marked stale[/] "
        "— the next plan will report no change for them even though state is "
        "wrong; run `atlantide refresh` before applying again:",
    )
    _per_node(
        report.orphaned,
        f"[bold red]{len(report.orphaned)} resource(s) left running untracked[/] "
        "— atlantide no longer has a state row for them, so nothing will find "
        "them again; delete them by hand:",
    )


def _per_node(rows: Mapping[str, str], header: str, *, footer: str = "") -> None:
    """A headline, then one ``node: reason`` line per node. Silent when empty.

    ``header`` carries its own markup: each of these highlights the count and
    leaves the explanation unstyled, and wrapping the whole line instead would
    turn a pointed warning into a wall of red.
    """
    if not rows:
        return
    console.print(header)
    for node_id, reason in rows.items():
        console.print(f"  [red]{escape(node_id)}[/]: {escape(reason)}")
    if footer:
        console.print(f"[dim]{footer}[/]")


def render_drift(
    report: DriftReport, *, wrote: bool, verbose: bool = False, pruned: bool = False
) -> None:
    """Group each node's drift by stack, showing changed outputs for DRIFTED nodes.

    Each verdict carries the coverage the read gave it. A provider whose ``read``
    reports only an id checked nothing, and printing a bare "in sync" for that
    node would assert something no API call established; the counts say how much
    of the resource the verdict actually covers, and ``verbose`` names the fields
    it did not.
    """
    node_of = {n.node_id: n for n in report.nodes}
    for node_id in stack_sections([n.node_id for n in report.nodes]):
        drift = node_of[node_id]
        sign, color, label = _DRIFT_SIGN[drift.kind]
        console.print(f"  [{color}]{sign} {label:<8}[/] {short_id(node_id)}{_coverage(drift)}")
        for field_name, (old, new) in drift.changed.items():
            console.print(
                f"      [dim]{escape(field_name)}: {fmt_value(old)} → {fmt_value(new)}[/]"
            )
        if verbose and drift.unobserved:
            names = ", ".join(escape(name) for name in drift.unobserved)
            console.print(f"      [dim]not checked: {names}[/]")
    n_drift = len(report.drifted)
    n_missing = len(report.missing)
    if not report.has_drift:
        console.print("\n[bold]Refresh:[/] no drift in the fields that were checked")
    else:
        parts = []
        if n_drift:
            parts.append(f"{n_drift} drifted")
        if n_missing:
            parts.append(f"{n_missing} missing")
        synced = " [green](state updated)[/]" if wrote else " [dim](state unchanged)[/]"
        console.print(f"\n[bold]Refresh:[/] {', '.join(parts)}{synced}")
    if n_missing and not pruned:
        # Kept deliberately: a failed read is not proof the resource is gone, and
        # the row is the only record of it.
        console.print(
            f"[dim]{n_missing} resource(s) could not be found; their state rows were "
            f"kept and marked for re-check. Confirm they are really gone, then "
            f"`refresh --write --prune` to forget them.[/]"
        )
    unchecked = sorted({n.node_id for n in report.nodes if n.unobserved})
    if unchecked and not verbose:
        console.print(
            f"[dim]{len(unchecked)} node(s) have fields this provider's read does not "
            f"report — pass --verbose to list them[/]"
        )


def _coverage(drift: NodeDrift) -> str:
    """ " (3 of 9 inputs checked)" — omitted when the read covered everything."""
    if drift.kind is Drift.MISSING or not drift.unobserved:
        return ""
    total = len(drift.observed) + len(drift.unobserved)
    return f" [dim]({len(drift.observed)} of {total} inputs checked)[/]"


#: sign, colour and label per import outcome, in the shape `_DRIFT_SIGN` uses.
#: Keyed by the whole enum, so adding a status without a sign fails the lookup
#: loudly rather than rendering a placeholder nobody notices.
_IMPORT_SIGN: dict[ImportStatus, tuple[str, str, str]] = {
    ImportStatus.IMPORTED: ("+", "green", "imported"),
    ImportStatus.WOULD_IMPORT: ("+", "cyan", "would import"),
    ImportStatus.ALREADY_TRACKED: ("=", "dim", "tracked"),
    ImportStatus.DRIFTED: ("!", "yellow", "drifted"),
    ImportStatus.NOT_FOUND: ("x", "red", "not found"),
    ImportStatus.BLOCKED: ("x", "red", "blocked"),
}


def render_import(outcomes: list[ImportOutcome], *, wrote: bool, verbose: bool = False) -> None:
    """One line per adopted node, with the field-level diff for anything drifted."""
    by_id = {o.node_id: o for o in outcomes}
    for node_id in stack_sections([o.node_id for o in outcomes]):
        outcome = by_id[node_id]
        sign, color, label = _IMPORT_SIGN[outcome.status]
        # The coverage suffix is not decoration. "imported" asserts that the live
        # resource matches config, and that claim is only as wide as the provider's
        # read: a handler reporting 1 of 4 inputs checked one of them. Saying so
        # inline — as the drift report does — keeps the claim honest without -v.
        console.print(
            f"  [{color}]{sign} {label:<12}[/] {short_id(node_id)}"
            f"{_bound_to(outcome)}{_coverage(outcome.drift) if outcome.drift else ''}"
        )
        if outcome.detail:
            console.print(f"      [dim]{escape(outcome.detail)}[/]")
        for field_name, (old, new) in (outcome.drift.changed if outcome.drift else {}).items():
            console.print(
                f"      [dim]config {fmt_value(old)} → live {fmt_value(new)}"
                f" ({escape(field_name)})[/]"
            )
        if verbose and outcome.unobserved:
            names = ", ".join(escape(name) for name in outcome.unobserved)
            console.print(f"      [dim]not checked: {names}[/]")

    imported = [o for o in outcomes if o.wrote_state]
    refused = [o for o in outcomes if o.unresolved]
    verb = "Imported" if wrote else "Would import"
    summary = f"{len(imported)} adopted" if wrote else f"{len(outcomes) - len(refused)} to adopt"
    console.print(f"\n[bold]{verb}:[/] {summary}, {len(refused)} refused")
    if imported:
        # Import only ever adds rows, so the undo is exact and worth naming: a
        # user who adopted the wrong resource wants it out of state, not destroyed.
        console.print(
            "[dim]Run `atlantide plan` to confirm no changes. To undo, "
            "`atlantide state rm <node>` forgets a row without touching the resource.[/]"
        )
    if any(o.status is ImportStatus.DRIFTED for o in outcomes):
        console.print(
            "[dim]A drifted resource was not imported: its live values differ from "
            "config, so importing it would mean the next apply changes it. Reconcile "
            "the config, or re-run with --allow-drift to adopt and see the update.[/]"
        )


def _bound_to(outcome: ImportOutcome) -> str:
    """The id an adopted node was bound to, when the type needed one."""
    if not outcome.external_id:
        return ""
    return f" [dim]({escape(outcome.identity_field or 'id')}={escape(outcome.external_id)})[/]"


def render_importable(node_ids: list[str], identity: dict[str, str | None]) -> None:
    """What could be adopted, and which of them need an id supplying."""
    if not node_ids:
        console.print("[dim]state already tracks every resource this config declares[/]")
        return
    for node_id in stack_sections(node_ids):
        field_name = identity.get(node_id)
        needs = f" [dim](needs an {escape(field_name)})[/]" if field_name else ""
        console.print(f"  [cyan]?[/] {short_id(node_id)}{needs}")
    console.print(f"\n[bold]Importable:[/] {len(node_ids)} resource(s) declared but not in state")
