"""The plan: what a config would change, as a Rich view and as ``--json``."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Mapping
from typing import Any, TypedDict

from rich.markup import escape
from rich.rule import Rule

from atlantide.cli.console import console
from atlantide.cli.views.common import (
    SECRET_REDACTED,
    SIGN,
    fmt_value,
    print_stack_sections,
    summary_bar,
)
from atlantide.core import PolicyLevel
from atlantide.core.actions import Action
from atlantide.core.markers import contains_ref
from atlantide.core.node_id import group_by_stack, short_id
from atlantide.engine import Compiled, Plan
from atlantide.reconcile import Change
from atlantide.secrets import is_secret_ref_marker

__all__ = [
    "PlanJson",
    "field_diffs",
    "lifecycle_changes",
    "plan_json",
    "render_declared_outputs",
    "render_destroy_preview",
    "render_envs",
    "render_inputs",
    "render_plan",
    "render_targeting",
    "render_violations",
    "render_warnings",
]


class PlanEnvsJson(TypedDict):
    declared: list[str]
    selected: list[str]


class PlanChangeJson(TypedDict):
    node_id: str
    action: str
    changed_fields: list[str]
    conditional: bool
    #: The ``changed_fields`` whose config is unchanged but whose ``$ref`` value
    #: moved since the node was last applied (an earlier run stopped before it).
    upstream_moved: list[str]
    create_before_destroy: bool
    #: A NOOP whose apply rewrites only the state row (a ``prevent_destroy`` change).
    state_only: bool
    #: Lifecycle flags this plan changes: ``{"prevent_destroy": [stored, desired]}``.
    lifecycle_changes: dict[str, list[bool]]


class PlanViolationJson(TypedDict):
    policy: str
    level: str
    node_id: str
    message: str


class PlanJson(TypedDict):
    """The ``plan --json`` document, in key order."""

    summary: dict[str, int]
    inputs: dict[str, Any]
    envs: PlanEnvsJson
    changes: list[PlanChangeJson]
    outputs: dict[str, Any]
    violations: list[PlanViolationJson]
    warnings: list[str]
    blocked: bool


def field_diffs(change: Change) -> list[str]:
    """``field: old → new`` lines for an UPDATE/REPLACE, from prior vs desired props."""
    if change.action not in (Action.UPDATE, Action.REPLACE):
        return []
    prior = change.prior.properties if change.prior else {}
    desired = change.desired.properties if change.desired else {}
    lines = []
    for field in change.changed_fields:
        if field in change.upstream_moved:
            # Same marker on both sides: what changed is the value it resolves to.
            lines.append(
                f"{field}: {fmt_value(desired.get(field))} "
                "(its value moved since this resource was last applied)"
            )
            continue
        lines.append(f"{field}: {fmt_value(prior.get(field))} → {fmt_value(desired.get(field))}")
    return lines


def lifecycle_changes(change: Change) -> dict[str, list[bool]]:
    """``{"prevent_destroy": [stored, desired]}`` when the plan changes the flag, else ``{}``.

    Only a node both declared and recorded has a change: a CREATE records the
    flag for the first time and a DELETE drops the row.
    """
    if change.desired is None or change.prior is None:
        return {}
    stored, wanted = change.prior.prevent_destroy, change.desired.prevent_destroy
    return {"prevent_destroy": [stored, wanted]} if stored != wanted else {}


def _lifecycle_lines(change: Change) -> list[str]:
    return [
        f"{flag}: {str(old).lower()} → {str(new).lower()}"
        for flag, (old, new) in lifecycle_changes(change).items()
    ]


def render_plan(plan_obj: Plan, *, targeted: bool = False, show_unchanged: bool = False) -> None:
    """The plan, grouped by stack.

    Unchanged (NOOP) rows are folded into one ``N unchanged`` line per stack
    unless ``show_unchanged``: in a large config they are nearly every row, and
    listing them buries the few that change. The summary still counts them, and
    ``--json`` always carries every row.
    """
    render_inputs(plan_obj.compiled.inputs)
    render_envs(plan_obj.compiled)
    if targeted:
        render_targeting(plan_obj)
    changeset = plan_obj.changeset
    changes = {c.node_id: c for c in changeset.changes}
    if show_unchanged:
        print_stack_sections(list(changes), lambda n: _plan_rows(changes[n]))
    else:
        _print_folded_sections(changes)
    counts = Counter(change.action for change in changeset.changes)
    state_only = sum(1 for change in changeset.changes if change.state_only)
    console.print(f"\n[bold]Plan:[/] {summary_bar(counts, state_only=state_only)}")
    render_declared_outputs(plan_obj.compiled.outputs)
    render_violations(plan_obj)
    render_warnings(plan_obj)


def _print_folded_sections(changes: Mapping[str, Change]) -> None:
    """:func:`~atlantide.cli.views.common.print_stack_sections` for a plan, with
    each stack's NOOP rows replaced by a single count line after its changes.

    A state-only NOOP is a change (it rewrites a row's ``prevent_destroy``), so it
    keeps its row."""
    for stack, ids in group_by_stack(list(changes)).items():
        console.print(Rule(f"[bold]{stack}[/]", align="left", style="dim"))
        shown = [node_id for node_id in ids if _is_shown(changes[node_id])]
        lines = [line for node_id in shown for line in _plan_rows(changes[node_id])]
        unchanged = len(ids) - len(shown)
        if unchanged:
            lines.append(f"  [dim]{unchanged} unchanged[/]")
        console.print("\n".join(lines), highlight=False)


def _is_shown(change: Change) -> bool:
    return change.action is not Action.NOOP or change.state_only


def _plan_rows(change: Change) -> Iterator[str]:
    if change.state_only:
        # `~` as for an update, labelled `state`: the row changes, the resource does not.
        sign, color = SIGN[Action.UPDATE]
        flags = ", ".join([*_lifecycle_lines(change), "state only"])
        yield rf"  [{color}]{sign} {'state':<7}[/] {short_id(change.node_id)}  [dim]\[{flags}][/]"
        return
    sign, color = SIGN[change.action]
    label = f"{change.action.value:<7}"
    yield f"  [{color}]{sign} {label}[/] {short_id(change.node_id)}{_plan_suffix(change)}"
    for line in field_diffs(change):
        yield f"      [dim]{escape(line)}[/]"
    if change.action is not Action.NOOP:
        for line in _lifecycle_lines(change):
            yield f"      [dim]{escape(line)}[/]"


def render_targeting(plan_obj: Plan) -> None:
    """State that this plan is a subset.

    Without this, a targeted plan showing no changes is indistinguishable from an
    up-to-date one.
    """
    total = len(plan_obj.compiled.graph.node_ids)
    acting = len(plan_obj.changeset.actionable)
    console.print(
        f"[yellow]targeting[/] {acting} change(s) across {total} resource(s) — "
        f"anything not selected is not shown and will not change"
    )


def render_inputs(inputs: dict[str, Any]) -> None:
    """The config inputs this plan was computed from.

    Two runs of the same config can plan differently when their inputs differ;
    showing them explains why.
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
    # ``\[`` escapes the bracket so Rich does not parse it as markup.
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
        console.print(f"[{color}]policy {tag}[/] {escape(v.policy)}: {escape(v.message)}")
    if plan_obj.blocked:
        n = len(plan_obj.blocked)
        console.print(f"[bold red]{n} mandatory policy violation(s) block apply[/]")


def render_destroy_preview(node_ids: list[str]) -> None:
    """List what a destroy will remove, grouped by stack, before the prompt."""
    sign, color = SIGN[Action.DELETE]
    print_stack_sections(node_ids, lambda n: (f"  [{color}]{sign} destroy[/] {short_id(n)}",))
    console.print(f"\n[bold]Plan:[/] {len(node_ids)} to destroy")


def plan_json(plan_obj: Plan) -> PlanJson:
    changeset = plan_obj.changeset
    counts = Counter(c.action for c in changeset.changes)
    return {
        "summary": {action.value: counts.get(action, 0) for action in Action},
        # Runs of the same config with different inputs can plan differently.
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
                "upstream_moved": list(c.upstream_moved),
                "create_before_destroy": c.create_before_destroy,
                "state_only": c.state_only,
                "lifecycle_changes": lifecycle_changes(c),
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
