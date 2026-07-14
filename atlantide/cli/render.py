"""Human-readable (Rich) rendering of plans, reports, and drift."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from typing import Any

from rich.markup import escape
from rich.rule import Rule

from atlantide.cli.console import console
from atlantide.core import PolicyLevel
from atlantide.core.fields import Mutability
from atlantide.core.markers import contains_ref
from atlantide.core.node_id import group_by_stack, short_id
from atlantide.engine import Plan
from atlantide.reconcile import Action, ApplyReport, Change, Drift, DriftReport
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


def render_plan(plan_obj: Plan) -> None:
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
    if report.rolled_back:
        console.print(f"[yellow]rolled back {len(report.rolled_back)} node(s)[/]")
    if report.outputs:
        console.print("\n[bold]Outputs:[/]")
        for key, value in report.outputs.items():
            redact = is_secret_ref_marker(value) or key in report.sensitive_outputs
            shown = SECRET_REDACTED if redact else escape(str(value))
            console.print(f"  {key} = {shown}")


def render_drift(report: DriftReport, *, wrote: bool) -> None:
    """Group each node's drift by stack, showing changed outputs for DRIFTED nodes."""
    node_of = {n.node_id: n for n in report.nodes}
    for node_id in stack_sections([n.node_id for n in report.nodes]):
        drift = node_of[node_id]
        sign, color, label = _DRIFT_SIGN[drift.kind]
        console.print(f"  [{color}]{sign} {label:<8}[/] {short_id(node_id)}")
        for field_name, (old, new) in drift.changed.items():
            console.print(
                f"      [dim]{escape(field_name)}: "
                f"{fmt_value(old)} → {fmt_value(new)}[/]"
            )
    n_drift = len(report.drifted)
    n_missing = len(report.missing)
    if not report.has_drift:
        console.print("\n[bold]Refresh:[/] no drift — state matches reality")
    else:
        parts = []
        if n_drift:
            parts.append(f"{n_drift} drifted")
        if n_missing:
            parts.append(f"{n_missing} missing")
        synced = " [green](state updated)[/]" if wrote else " [dim](state unchanged)[/]"
        console.print(f"\n[bold]Refresh:[/] {', '.join(parts)}{synced}")
