"""Machine-readable ``--json`` output for plan, apply, and refresh."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from typing import Any

from atlantide.cli.console import console
from atlantide.core.markers import contains_ref
from atlantide.engine import Plan
from atlantide.reconcile import Action, ApplyReport, DriftReport
from atlantide.reconcile.adopt import ImportOutcome
from atlantide.secrets import is_secret_ref_marker


def plan_json(plan_obj: Plan) -> dict[str, Any]:
    changeset = plan_obj.changeset
    counts = Counter(c.action for c in changeset.changes)
    return {
        "summary": {action.value: counts.get(action, 0) for action in Action},
        # What the config was evaluated with; two runs differing only in these
        # legitimately plan differently.
        "inputs": plan_obj.compiled.inputs,
        # Declared environments and the subset this run acted on: equal when
        # nothing was narrowed, both empty when the config has no `Config`.
        "envs": {
            "declared": list(plan_obj.compiled.envs_declared),
            "selected": list(plan_obj.compiled.envs_selected),
        },
        "changes": [
            {
                "node_id": c.node_id,
                "action": c.action.value,
                "changed_fields": list(c.changed_fields),
                "conditional": c.conditional,
                "create_before_destroy": c.create_before_destroy,
            }
            for c in changeset.changes
        ],
        "outputs": {
            k: (None if contains_ref(v) or is_secret_ref_marker(v) else v)
            for k, v in plan_obj.compiled.outputs.items()
        },
        "violations": [
            {"policy": v.policy, "level": v.level.value, "node_id": v.node_id, "message": v.message}
            for v in plan_obj.violations
        ],
        "warnings": list(plan_obj.warnings),
        "blocked": bool(plan_obj.blocked),
    }


def report_json(report: ApplyReport) -> dict[str, Any]:
    return {
        "created": report.created,
        "updated": report.updated,
        "replaced": report.replaced,
        "deleted": report.deleted,
        "noop": report.noop,
        "rolled_back": report.rolled_back,
        "rollback_failed": report.rollback_failed,
        # Rows that could not be marked stale: the next plan will report NOOP for
        # them despite state being wrong. Orphans have no row left at all.
        "poison_failed": report.poison_failed,
        "orphaned": report.orphaned,
        # Non-null when the saga was deliberately not run (see ApplyReport).
        "rollback_skipped": report.rollback_skipped,
        "outputs": {
            k: (None if is_secret_ref_marker(v) or k in report.sensitive_outputs else v)
            for k, v in report.outputs.items()
        },
    }


def drift_json(report: DriftReport) -> dict[str, Any]:
    return {
        "drift": report.has_drift,
        "nodes": [
            {
                "node_id": n.node_id,
                "kind": n.kind.value,
                "changed": {k: {"state": old, "live": new} for k, (old, new) in n.changed.items()},
                # The verdict's scope: fields the provider's read did not report
                # were not checked, so `kind` says nothing about them.
                "observed": list(n.observed),
                "unobserved": list(n.unobserved),
            }
            for n in report.nodes
        ],
    }


def import_json(outcomes: list[ImportOutcome]) -> dict[str, Any]:
    """One document describing every adopted node, shaped like :func:`drift_json`."""
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
) -> dict[str, Any]:
    """What `atlantide import` with no arguments answers, as a document.

    Beside :func:`import_json` rather than inline in the command, so both halves
    of the import contract are versioned by the same ``schema_version``.
    """
    return {
        "importable": [
            {"node_id": nid, "type": types[nid], "identity_field": identity[nid]}
            for nid in node_ids
        ]
    }


#: Bumped when the shape of a payload changes incompatibly, so a consumer can
#: refuse a document it does not understand instead of silently misreading it.
SCHEMA_VERSION = 1


def emit_json(payload: dict[str, Any]) -> None:
    """Write one JSON document to stdout: the whole of this command's output."""
    console.print_json(
        json.dumps({"schema_version": SCHEMA_VERSION, "ok": True, **payload}, default=str)
    )


def error_json(err: BaseException, *, state: str | None = None) -> dict[str, Any]:
    """The failure envelope, in the same shape a success uses.

    Without this, ``--json`` emits a parseable document when a command succeeds
    and Rich-formatted text when it fails — so a CI consumer gets valid JSON or
    garbage depending on the outcome, and the outcome it most needs to read is
    the failing one.
    """
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "error": {
            "kind": type(err).__name__,
            "message": str(err),
            **{
                key: value
                for key in ("node_id", "op", "resource_type", "line", "col")
                if (value := getattr(err, key, None)) is not None
            },
            "also_failed": [
                {"kind": type(other).__name__, "message": str(other)}
                for other in getattr(err, "_also_failed", [])
            ],
        },
    }
    if state is not None:
        payload["state"] = state
    return payload


def emit_error_json(err: BaseException, *, state: str | None = None) -> None:
    console.print_json(json.dumps(error_json(err, state=state), default=str))


def emit_or_render(
    json_out: bool,
    *,
    payload: Callable[[], dict[str, Any]],
    render: Callable[[], None],
    state: str,
) -> None:
    """Emit the JSON document or render the human view — one shape, four commands.

    The ``state`` rider is part of every machine-readable document: with a shared
    backend, "which state was this?" is the question a parsed result cannot
    answer for itself.
    """
    if json_out:
        emit_json({**payload(), "state": state})
    else:
        render()
