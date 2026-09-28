"""What ``refresh`` found: live state against recorded state, as a Rich view and as ``--json``."""

from __future__ import annotations

from typing import Any, TypedDict

from rich.markup import escape

from atlantide.cli.console import console
from atlantide.cli.views.common import coverage_note, fmt_value, stack_sections
from atlantide.core.node_id import short_id
from atlantide.reconcile import Drift, DriftReport

__all__ = ["DriftJson", "drift_json", "render_drift"]


class DriftFieldJson(TypedDict):
    state: Any
    live: Any


class DriftNodeJson(TypedDict):
    node_id: str
    kind: str
    changed: dict[str, DriftFieldJson]
    observed: list[str]
    unobserved: list[str]


class DriftJson(TypedDict):
    """The ``refresh --json`` document, in key order."""

    drift: bool
    nodes: list[DriftNodeJson]


_DRIFT_SIGN = {
    Drift.IN_SYNC: ("=", "dim", "in sync"),
    Drift.DRIFTED: ("~", "yellow", "drifted"),
    Drift.MISSING: ("-", "red", "missing"),
}


def render_drift(
    report: DriftReport, *, wrote: bool, verbose: bool = False, pruned: bool = False
) -> None:
    """Group each node's drift by stack, showing changed outputs for DRIFTED nodes.

    Each verdict shows how many inputs the read covered, since a provider whose
    ``read`` reports only an id checks nothing. ``verbose`` names the unchecked
    fields.
    """
    node_of = {n.node_id: n for n in report.nodes}
    for node_id in stack_sections([n.node_id for n in report.nodes]):
        drift = node_of[node_id]
        sign, color, label = _DRIFT_SIGN[drift.kind]
        console.print(f"  [{color}]{sign} {label:<8}[/] {short_id(node_id)}{coverage_note(drift)}")
        for field_name, (old, new) in drift.changed.items():
            console.print(
                f"      [dim]{escape(field_name)}: {fmt_value(old)} → {fmt_value(new)}[/]"
            )
        if verbose and drift.unobserved:
            names = ", ".join(escape(name) for name in drift.unobserved)
            console.print(f"      [dim]not checked: {names}[/]")
    console.print(_summary_line(report, wrote=wrote))
    _footnotes(report, verbose=verbose, pruned=pruned)


def _summary_line(report: DriftReport, *, wrote: bool) -> str:
    """The ``Refresh:`` line: what was found, and whether state now reflects it."""
    if not report.has_drift:
        return "\n[bold]Refresh:[/] no drift in the fields that were checked"
    n_drift, n_missing = len(report.drifted), len(report.missing)
    parts = []
    if n_drift:
        parts.append(f"{n_drift} drifted")
    if n_missing:
        parts.append(f"{n_missing} missing")
    synced = " [green](state updated)[/]" if wrote else " [dim](state unchanged)[/]"
    return f"\n[bold]Refresh:[/] {', '.join(parts)}{synced}"


def _footnotes(report: DriftReport, *, verbose: bool, pruned: bool) -> None:
    """What the summary cannot say: rows kept for a failed read, and unchecked fields."""
    n_missing = len(report.missing)
    if n_missing and not pruned:
        # Kept: a failed read is not proof the resource is gone, and the row is
        # the only record of it.
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


def drift_json(report: DriftReport) -> DriftJson:
    return {
        "drift": report.has_drift,
        "nodes": [
            {
                "node_id": n.node_id,
                "kind": n.kind.value,
                "changed": {k: {"state": old, "live": new} for k, (old, new) in n.changed.items()},
                # Fields the provider's read does not report are unchecked; `kind`
                # does not cover them.
                "observed": list(n.observed),
                "unobserved": list(n.unobserved),
            }
            for n in report.nodes
        ],
    }
