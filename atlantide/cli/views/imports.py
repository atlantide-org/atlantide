"""What ``import`` adopted, or could adopt, as a Rich view and as ``--json``."""

from __future__ import annotations

from typing import Any, TypedDict

from rich.markup import escape

from atlantide.cli.console import console
from atlantide.cli.views.common import coverage_note, fmt_value, stack_sections
from atlantide.core.node_id import short_id
from atlantide.reconcile.adopt import ImportOutcome, ImportStatus

__all__ = [
    "ImportJson",
    "ImportableJson",
    "import_json",
    "importable_json",
    "render_import",
    "render_importable",
]


class ImportDriftJson(TypedDict):
    config: Any
    live: Any


class ImportNodeJson(TypedDict):
    node_id: str
    type: str
    status: str
    identity_field: str | None
    external_id: str | None
    recorded: list[str]
    drift: dict[str, ImportDriftJson]
    unobserved: list[str]
    detail: str


class ImportJson(TypedDict):
    """The ``import <node> --json`` document, in key order."""

    imported: int
    refused: int
    nodes: list[ImportNodeJson]


class ImportableEntryJson(TypedDict):
    node_id: str
    type: str
    identity_field: str | None


class ImportableJson(TypedDict):
    """The ``import --json`` document (no node named), in key order."""

    importable: list[ImportableEntryJson]


#: Sign, colour and label per import status, as in the drift view. Covers every
#: status, so a new status without an entry fails the lookup instead of rendering
#: a placeholder.
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
        # "imported" asserts a match only for the fields the provider's read
        # reports; the coverage suffix shows how many were checked without -v.
        console.print(
            f"  [{color}]{sign} {label:<12}[/] {short_id(node_id)}"
            f"{_bound_to(outcome)}{coverage_note(outcome.drift) if outcome.drift else ''}"
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
        # Import only adds rows, so `state rm` is an exact undo: it removes a
        # wrongly adopted resource from state without destroying it.
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


def import_json(outcomes: list[ImportOutcome]) -> ImportJson:
    """One document describing every adopted node, shaped like
    :func:`~atlantide.cli.views.drift.drift_json`."""
    return {
        "imported": sum(1 for o in outcomes if o.wrote_state),
        "refused": sum(1 for o in outcomes if o.unresolved),
        "nodes": [
            {
                "node_id": o.node_id,
                "type": o.type,
                "status": o.status.value,
                "identity_field": o.identity_field,
                "external_id": o.external_id,
                # Names only: a recorded value may be a sealed secret.
                "recorded": list(o.recorded),
                "drift": {
                    k: {"config": old, "live": new}
                    for k, (old, new) in (o.drift.changed if o.drift else {}).items()
                },
                "unobserved": list(o.unobserved),
                "detail": o.detail,
            }
            for o in outcomes
        ],
    }


def importable_json(
    node_ids: list[str], types: dict[str, str], identity: dict[str, str | None]
) -> ImportableJson:
    """The ``import --json`` document when no node is named.

    Kept beside :func:`import_json` so both import documents share one
    ``schema_version``.
    """
    return {
        "importable": [
            {"node_id": nid, "type": types[nid], "identity_field": identity[nid]}
            for nid in node_ids
        ]
    }
