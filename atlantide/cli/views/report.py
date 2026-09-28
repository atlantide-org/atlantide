"""What an apply, deploy or destroy did, as a Rich view and as ``--json``."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any, TypedDict

from rich.markup import escape

from atlantide.cli.console import console
from atlantide.cli.views.common import SECRET_REDACTED, SIGN, print_stack_sections, summary_bar
from atlantide.core.actions import Action
from atlantide.core.node_id import short_id
from atlantide.reconcile import ApplyReport
from atlantide.secrets import is_secret_ref_marker

__all__ = ["ReportJson", "render_report", "report_json"]


class ReportJson(TypedDict):
    """The ``apply --json`` / ``deploy --json`` document, in key order."""

    created: list[str]
    updated: list[str]
    replaced: list[str]
    deleted: list[str]
    noop: list[str]
    state_only: list[str]
    downgraded: dict[str, str]
    rolled_back: list[str]
    rollback_failed: dict[str, str]
    poison_failed: dict[str, str]
    orphaned: dict[str, str]
    rollback_skipped: str | None
    outputs: dict[str, Any]


def render_report(
    report: ApplyReport,
    elapsed: float | None = None,
    *,
    title: str = "Applied",
    summary: str | None = None,
    show_nodes: bool = True,
) -> None:
    # Per-node rows use the plan's signs, grouped by stack. Pass show_nodes=False
    # when a live progress table already printed them.
    if show_nodes:
        action_of = {
            **dict.fromkeys(report.created, Action.CREATE),
            **dict.fromkeys(report.updated, Action.UPDATE),
            **dict.fromkeys(report.replaced, Action.REPLACE),
            **dict.fromkeys(report.deleted, Action.DELETE),
        }

        state_only = set(report.state_only)

        def done_row(node_id: str) -> tuple[str]:
            if node_id in state_only:
                sign, color = SIGN[Action.UPDATE]
                return (f"  [{color}]{sign} done[/] {short_id(node_id)}  [dim](state only)[/]",)
            sign, color = SIGN[action_of[node_id]]
            return (f"  [{color}]{sign} done[/] {short_id(node_id)}",)

        print_stack_sections([*action_of, *report.state_only], done_row)
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
    bar = summary_bar(counts, state_only=len(report.state_only))
    console.print(f"\n[bold]{title}:[/] {summary or bar}{took}")
    _render_downgraded(report)
    _render_trouble(report)
    if report.outputs:
        console.print("\n[bold]Outputs:[/]")
        for key, value in report.outputs.items():
            redact = is_secret_ref_marker(value) or key in report.sensitive_outputs
            shown = SECRET_REDACTED if redact else escape(str(value))
            console.print(f"  {key} = {shown}")


def _render_downgraded(report: ApplyReport) -> None:
    """Name the known-after-apply replaces the apply did not need."""
    if not report.downgraded:
        return
    console.print(
        f"[dim]{len(report.downgraded)} known-after-apply replace(s) not needed "
        "— no immutable value changed:[/]"
    )
    for node_id, action in report.downgraded.items():
        console.print(f"  [dim]{escape(short_id(node_id))}: {action} instead of replace[/]")


def _render_trouble(report: ApplyReport) -> None:
    """Print what went wrong during the run, most severe last.

    Each case leaves state and the live resources further apart than the one
    before it.
    """
    if report.rolled_back:
        console.print(f"[yellow]rolled back {len(report.rolled_back)} node(s)[/]")
    if report.rollback_skipped:
        # Not a failure, but resources this run created remain.
        console.print(
            f"[bold red]rollback skipped[/] — {escape(report.rollback_skipped)}\n"
            "[dim]resources this run created were left in place; run "
            "`atlantide refresh` to see what exists[/]"
        )
    # An incomplete rollback leaves state and the provider disagreeing. The rows
    # are marked stale so the next plan re-diffs them, but they still need manual
    # review.
    _per_node(
        report.rollback_failed,
        f"[bold red]rollback incomplete for {len(report.rollback_failed)} node(s)[/] "
        "— state may not describe the live resources:",
        footer="these rows are marked stale, so the next plan will re-check them "
        "instead of reporting no change",
    )
    # The stale mark failed, so the next plan reports these as NOOP.
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

    ``header`` carries its own markup so each caller highlights only the count
    and leaves the explanation unstyled.
    """
    if not rows:
        return
    console.print(header)
    for node_id, reason in rows.items():
        console.print(f"  [red]{escape(node_id)}[/]: {escape(reason)}")
    if footer:
        console.print(f"[dim]{footer}[/]")


def report_json(report: ApplyReport) -> ReportJson:
    return {
        "created": report.created,
        "updated": report.updated,
        "replaced": report.replaced,
        "deleted": report.deleted,
        "noop": report.noop,
        # Rows rewritten without a provider call (a `prevent_destroy` change);
        # also listed under `noop`.
        "state_only": report.state_only,
        # Conditional REPLACEs the apply found unnecessary: node id -> the action
        # taken instead ("update" or "noop"), under which the node is listed.
        "downgraded": report.downgraded,
        "rolled_back": report.rolled_back,
        "rollback_failed": report.rollback_failed,
        # Rows that could not be marked stale: the next plan reports NOOP for them
        # although state is wrong. Orphans have no row at all.
        "poison_failed": report.poison_failed,
        "orphaned": report.orphaned,
        # Why the rollback saga did not run, or None if it ran (see ApplyReport).
        "rollback_skipped": report.rollback_skipped,
        "outputs": {
            k: (None if is_secret_ref_marker(v) or k in report.sensitive_outputs else v)
            for k, v in report.outputs.items()
        },
    }
