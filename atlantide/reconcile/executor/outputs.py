"""Committing a run's declared stack outputs once its mutations have landed."""

from __future__ import annotations

from typing import Any

from atlantide.core.node_id import stack_of
from atlantide.reconcile.env import ApplyEnv, Desired, LiveOutputs
from atlantide.reconcile.report import ApplyReport
from atlantide.reconcile.resolve import resolve_value, sensitive_output_names
from atlantide.secrets import SecretsRegistry
from atlantide.state import StateBackend, StateGraph


def commit_outputs(
    *,
    desired: Desired,
    env: ApplyEnv,
    live: LiveOutputs,
    prior: StateGraph,
    report: ApplyReport,
) -> None:
    """Resolve the declared exports and persist the committed stack outputs.

    Exports resolve against live outputs; unchanged nodes resolve from the
    prior-state seed the run started from. A restricted (targeted) run NOOPs
    unselected CREATEs, so an output over one has no value yet: it is skipped and
    its committed value is left untouched, since the infrastructure mutations have
    already succeeded. Sensitive exports are sealed at rest; the report holds them
    in the clear and redacts at the render boundary.
    """
    resolved_outputs: dict[str, Any] = {}
    for name, value in desired.output_decls.items():
        try:
            resolved_outputs[name] = resolve_value(value, live)
        except KeyError:
            continue
    report.outputs = resolved_outputs
    report.sensitive_outputs = sensitive_output_names(desired.output_decls, env)
    env.backend.set_outputs(
        persistable_outputs(report, env.secrets),
        remove=retired_outputs(desired, prior, report, env.backend),
    )


def retired_outputs(
    desired: Desired, prior: StateGraph, report: ApplyReport, backend: StateBackend
) -> list[str]:
    """Committed output keys this run owns but no longer declares.

    ``set_outputs`` merges, so unpruned keys persist: a dropped ``output()`` or a
    destroyed stack keeps its last value, and a dependent stack's
    ``StackReference`` resolves to a deleted resource. Scoped to this run's own
    stacks, since other configs' outputs share the store.
    """
    mine = {stack_of(node_id) for node_id in desired.resources}
    mine |= {stack_of(node_id) for node_id in prior.nodes}
    # Includes unresolved declarations: an output skipped under a restricted run
    # is still declared, so its committed value must be kept.
    declared = set(report.outputs) | set(desired.output_decls)
    return sorted(key for key in backend.outputs() if stack_of(key) in mine and key not in declared)


def persistable_outputs(report: ApplyReport, secrets: SecretsRegistry) -> dict[str, Any]:
    """The report's resolved outputs as stored: sensitive string values sealed."""
    return {
        name: (
            secrets.seal(value)
            if name in report.sensitive_outputs and isinstance(value, str)
            else value
        )
        for name, value in report.outputs.items()
    }
