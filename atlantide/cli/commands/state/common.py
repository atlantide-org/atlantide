"""What every ``state`` subcommand shares: the target, and whole-state reads and writes."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from atlantide.cli.context import set_json_mode
from atlantide.cli.target import StateTarget, current_project, resolve_target
from atlantide.state import StateBackend, StateGraph, StateNode
from atlantide.state.codec import StateDocument

__all__ = ["announced_target", "quiet_target", "replace_contents", "snapshot_document"]


def announced_target(state: Path | None = None) -> StateTarget:
    """Resolve this invocation's state target and announce it, in human output mode."""
    set_json_mode(enabled=False)
    return resolve_target(state, current_project())


def quiet_target(state: Path | None = None) -> StateTarget:
    """Resolve the state target without the banner, for ``--json`` output.

    The JSON document carries the target as a field instead. JSON mode routes
    later warnings and errors to stderr rather than into the payload. Both this
    and :func:`announced_target` set the mode explicitly: ``state`` subcommands
    choose it per command rather than inheriting the root flag.
    """
    set_json_mode(enabled=True)
    return resolve_target(state, current_project(), announce=False)


# -- whole-state reads and writes ---------------------------------------------


def snapshot_document(backend: StateBackend, graph: StateGraph) -> StateDocument:
    """``graph`` with the serial and outputs beside it, as a snapshot records them."""
    return StateDocument(
        serial=backend.serial(),
        nodes=dict(graph.nodes),
        outputs=backend.outputs(),
    )


def replace_contents(
    backend: StateBackend, nodes: Mapping[str, StateNode], outputs: Mapping[str, Any]
) -> None:
    """Make ``backend`` hold exactly ``nodes`` and ``outputs``. Call under the lock.

    Replace, not merge: nodes and outputs only the destination holds are removed.
    An upsert (``put_many``) would leave them as phantom nodes that the next apply
    or destroy acts on.
    """
    obsolete = sorted(set(backend.load().nodes) - set(nodes))
    backend.replace_many(obsolete, nodes.values())
    stale_outputs = sorted(set(backend.outputs()) - set(outputs))
    if outputs or stale_outputs:
        backend.set_outputs(outputs, remove=stale_outputs)
