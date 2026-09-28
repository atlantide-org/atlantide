"""Execute a ChangeSet against real providers, persisting state per node.

Applies (create/update/replace) run forward over the desired graph; deletes run
in reverse over the prior-state graph. Both use the parallel scheduler, so
independent work overlaps while dependencies are respected. A resource is
deleted after what depended on it in prior state and is destroyed this run: the
deletes a destroy-before-create REPLACE must wait for run first, and the rest,
with the old halves of create-before-destroy REPLACEs, run in one pass after the
forward pass. Two exceptions remain: a *conditional* REPLACE dependent is
destroyed, if confirmed, after its upstream's delete half, and so is the old
half of a create-before-destroy dependent of a destroy-before-create REPLACE.

Guarantees:
- **incremental persist** - each node's state row is written the moment its CRUD
  succeeds, so a crash leaves a consistent, resumable state;
- **failure handling** via ``on_failure``:
  - ``"halt"`` (default): the first provider error cancels the rest; completed
    nodes stay applied, resumable on the next apply;
  - ``"rollback"``: a **compensation saga** - each completed node records an undo
    action; on failure the executor runs them in reverse completion order, then
    re-raises. Only fully-completed nodes are compensated;
- **REPLACE** is destroy-before-create by default; a ``create_before_destroy``
  REPLACE creates the new resource in the forward pass and defers destroying the
  old one to the terminal delete pass (no downtime). A *conditional* REPLACE is
  first re-diffed against its upstreams' new outputs and runs as an UPDATE or a
  NOOP when no immutable value moved (reported under ``downgraded``). The diff
  never leaves one conditional behind a destroy-before-create upstream, and a
  node whose delete half already ran is never downgraded;
- **state-only** NOOPs (a ``prevent_destroy`` change) rewrite their row without
  a provider call.

Refs are resolved to concrete upstream outputs just before each provider call.

The run itself is :class:`~atlantide.reconcile.executor.run.ChangeSetRun`, which
orchestrates the phases over one shared
:mod:`~atlantide.reconcile.executor.context`; the apply-time re-check is
:mod:`~atlantide.reconcile.executor.confirm`, the REPLACE strategies are
:mod:`~atlantide.reconcile.executor.replace`, and the deletes are
:mod:`~atlantide.reconcile.executor.deletes`. Its state rows are
:mod:`~atlantide.reconcile.executor.records`, the saga is
:mod:`~atlantide.reconcile.executor.saga`, and output commit is
:mod:`~atlantide.reconcile.executor.outputs`.
"""

from __future__ import annotations

from atlantide.reconcile.changes import ChangeSet
from atlantide.reconcile.env import ApplyEnv, Desired, OnFailure
from atlantide.reconcile.executor.run import ChangeSetRun
from atlantide.reconcile.progress import ProgressCallback, no_progress
from atlantide.reconcile.report import ApplyReport
from atlantide.state import StateGraph

__all__ = ["apply"]


async def apply(
    *,
    changeset: ChangeSet,
    desired: Desired,
    prior: StateGraph,
    env: ApplyEnv,
    on_failure: OnFailure = "halt",
    progress: ProgressCallback | None = None,
) -> ApplyReport:
    """Run the ChangeSet; return a per-action report. Raises on provider failure.

    ``on_failure="rollback"`` runs a compensation saga before re-raising (see the
    module docstring); ``"halt"`` (default) leaves completed nodes applied.
    ``progress(node_id, action, phase)`` is called on each node's start/finish/fail.
    """
    return await ChangeSetRun(
        changeset=changeset,
        desired=desired,
        prior=prior,
        env=env,
        on_failure=on_failure,
        on_progress=progress or no_progress,
    ).run()
