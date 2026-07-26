"""Undo factories for the executor's compensation saga.

Each returns the coroutine factory recorded after a node completes; on failure
the executor runs them in reverse completion order. Kept beside — not inside —
the executor because they close over provider/backend/context alone and never
touch the run's shared mutable state.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from atlantide.core.context import Context
from atlantide.core.provider import Provider
from atlantide.core.resource import Resource
from atlantide.state.backend import StateBackend, StateNode

#: What an undo is once built: awaited with no arguments, reports nothing.
Undo = Callable[[], Awaitable[None]]


def _with_outputs(res: Resource, outputs: dict[str, Any]) -> Resource:
    """``res`` with the computed outputs of the create (notably its id) restored.

    A compensation deletes the resource just created; the id lets the provider act
    on it directly rather than locating it by attributes, which can match an
    unrelated resource sharing those attributes (e.g. a VPC CIDR).
    """
    fields = type(res).model_fields
    updates = {key: value for key, value in outputs.items() if key in fields}
    return res.model_copy(update=updates) if updates else res


@dataclass(frozen=True, slots=True)
class Compensator:
    """The three things every undo needs, bound once per node being applied.

    An undo is always the same shape — a provider call against a resource,
    followed by the state write that makes the provider's world and the recorded
    one agree again — so what varies between them is only which call and which
    row. Binding the invariant half here keeps that difference the only thing a
    call site spells out.
    """

    provider: Provider
    ctx: Context
    backend: StateBackend

    def undo_create(self, res: Resource, node_id: str) -> Undo:
        async def undo() -> None:
            await self.provider.delete(self.ctx, res)
            self.backend.delete(node_id)

        return undo

    def undo_update(
        self, old: Resource, prior_node: StateNode, prior_outputs: dict[str, Any]
    ) -> Undo:
        async def undo() -> None:
            await self.provider.update(self.ctx, prior_outputs, old)  # plaintext for the provider
            self.backend.put(prior_node)  # restore the prior (sealed) state row verbatim

        return undo

    def undo_replace(
        self, new: Resource, old: Resource, restore: Callable[[dict[str, Any]], None]
    ) -> Undo:
        """Undo a destroy-before-create REPLACE by recreating the original.

        ``restore`` records the re-create's own outputs. The prior state row cannot
        be written back verbatim: it names a physical id that no longer exists, so
        refresh would report the node MISSING and the recreated resource would be
        untracked.
        """

        async def undo() -> None:
            await self.provider.delete(self.ctx, new)
            recreated = await self.provider.create(self.ctx, old)  # a fresh id, not the prior one
            restore(recreated)

        return undo

    def undo_cbd_create(self, new: Resource, prior_node: StateNode) -> Undo:
        """Undo a create-before-destroy REPLACE's forward half.

        The old resource is still live (its deletion is deferred to cleanup), so
        undo removes the freshly-created replacement and restores the prior state
        row. The companion row is dropped with it: once the primary row describes
        the old resource again, a leftover companion would plan a DELETE of that
        same live resource.
        """

        async def undo() -> None:
            await self.provider.delete(self.ctx, new)
            self.backend.put(prior_node)
            self.backend.delete(_cbd_old_id(prior_node.id))

        return undo


def _cbd_old_id(node_id: str) -> str:
    """Companion state id recording the still-live old half of a CBD REPLACE."""
    return f"{node_id}~replaced"
