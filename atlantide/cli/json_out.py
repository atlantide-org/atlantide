"""Machine-readable ``--json`` output for plan, apply, and refresh."""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from atlantide.cli.console import console
from atlantide.core.markers import contains_ref
from atlantide.engine import Plan
from atlantide.reconcile import Action, ApplyReport, DriftReport
from atlantide.secrets import is_secret_ref_marker


def plan_json(plan_obj: Plan) -> dict[str, Any]:
    changeset = plan_obj.changeset
    counts = Counter(c.action for c in changeset.changes)
    return {
        "summary": {action.value: counts.get(action, 0) for action in Action},
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
            }
            for n in report.nodes
        ],
    }


def emit_json(payload: dict[str, Any]) -> None:
    console.print_json(json.dumps(payload, default=str))
