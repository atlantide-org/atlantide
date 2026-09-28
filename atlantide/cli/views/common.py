"""The visual vocabulary every view shares: action signs, value display, stack sections."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from rich.rule import Rule

from atlantide.cli.console import console
from atlantide.core.actions import Action
from atlantide.core.fields import Mutability
from atlantide.core.markers import contains_ref
from atlantide.core.node_id import group_by_stack
from atlantide.reconcile import Drift, NodeDrift
from atlantide.secrets import is_secret_ref_marker

__all__ = [
    "MUT_COLOR",
    "SECRET_REDACTED",
    "SIGN",
    "coverage_note",
    "fmt_value",
    "print_stack_sections",
    "stack_sections",
    "summary_bar",
]

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

# Terraform-style summary buckets.
_SUMMARY_BUCKET = {
    Action.CREATE: "add",
    Action.UPDATE: "change",
    Action.REPLACE: "change",
    Action.DELETE: "destroy",
}


def summary_bar(counts: Counter[Action], *, state_only: int = 0) -> str:
    """A ``2 to add, 1 to change, 1 to destroy`` line (plus unchanged if any).

    ``state_only`` of the NOOPs rewrite only their state row (a
    ``prevent_destroy`` change); they are counted apart from the unchanged ones.
    """
    totals: Counter[str] = Counter()
    for action, n in counts.items():
        if action is not Action.NOOP:
            totals[_SUMMARY_BUCKET[action]] += n
    parts = [f"{totals[b]} to {b}" for b in ("add", "change", "destroy") if totals[b]]
    if state_only:
        parts.append(f"{state_only} state-only")
    unchanged = counts.get(Action.NOOP, 0) - state_only
    if unchanged > 0:
        parts.append(f"{unchanged} unchanged")
    return ", ".join(parts) or "no changes"


def fmt_value(value: Any, limit: int = 60) -> str:
    """A short display of a property value; secrets and unresolved refs are masked."""
    if is_secret_ref_marker(value):
        return SECRET_REDACTED
    if contains_ref(value):
        return "(known after apply)"
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def print_stack_sections(node_ids: list[str], rows: Callable[[str], Iterable[str]]) -> None:
    """Print ``rows(node_id)`` for every node, grouped under per-stack Rule headers.

    Equivalent to printing each row from :func:`stack_sections`, but each stack's
    rows go to Rich as one block: per-line ``console.print`` dominates wall time
    for a plan with thousands of resources. Highlighting is off because every row
    carries its own markup.
    """
    for stack, ids in group_by_stack(node_ids).items():
        console.print(Rule(f"[bold]{stack}[/]", align="left", style="dim"))
        console.print("\n".join(line for node_id in ids for line in rows(node_id)), highlight=False)


def stack_sections(node_ids: list[str]) -> Iterator[str]:
    """Yield node ids grouped by stack, printing each stack's Rule header first."""
    for stack, ids in group_by_stack(node_ids).items():
        console.print(Rule(f"[bold]{stack}[/]", align="left", style="dim"))
        yield from ids


def coverage_note(drift: NodeDrift) -> str:
    """The ``(3 of 9 inputs checked)`` suffix; empty when the read covered every input."""
    if drift.kind is Drift.MISSING or not drift.unobserved:
        return ""
    total = len(drift.observed) + len(drift.unobserved)
    return f" [dim]({len(drift.observed)} of {total} inputs checked)[/]"
