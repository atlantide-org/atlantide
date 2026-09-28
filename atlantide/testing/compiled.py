"""``Compiled`` and ``Plan``: one evaluation of a config, and the run it plans.

Every stage is the engine's own: :func:`~atlantide.engine.compiler.compile_registry`
lowers and hashes, :func:`~atlantide.reconcile.diff` classifies,
:func:`~atlantide.reconcile.check_prevent_destroy` guards (over the planner's own
:func:`~atlantide.engine.planner.protected_ids`), and the mutability table
comes from :func:`~atlantide.reconcile.type_mutability`, as the
:class:`~atlantide.engine.Engine` builds it. Nothing is applied and no provider is
called.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from functools import cache

from returns.result import Result

from atlantide.core import (
    PreventDestroyError,
    ProviderRegistry,
    RegistryError,
    Resource,
    Stack,
    collecting,
)
from atlantide.core.fields import Mutability
from atlantide.core.node_id import local_name_of
from atlantide.engine.compiler import compile_registry
from atlantide.engine.planner import protected_ids
from atlantide.engine.result import raise_on_failure
from atlantide.ir import IRGraph, IRNode, canonical_bytes
from atlantide.providers.loader import discover
from atlantide.reconcile import (
    Action,
    Change,
    ChangeSet,
    check_prevent_destroy,
    diff,
    type_mutability,
)
from atlantide.reconcile.changes import TypeMutability
from atlantide.state import StateGraph, StateNode

#: The stack name :func:`stack` and :meth:`Compiled.of` default to.
DEFAULT_STACK = "infra"
#: The cloud-name prefix :func:`stack` and :meth:`Compiled.of` default to.
DEFAULT_NAME_PREFIX = "acme"


@contextmanager
def stack(
    name: str = DEFAULT_STACK,
    *,
    region: str,
    name_prefix: str | None = DEFAULT_NAME_PREFIX,
    tags: dict[str, str] | None = None,
) -> Iterator[Stack]:
    """Open a :class:`~atlantide.core.Stack` to build a component in."""
    with Stack(name, region=region, name_prefix=name_prefix, tags=tags) as opened:
        yield opened


def local_names(*children: str, name: str) -> frozenset[str]:
    """The local names ``children`` take inside a component called ``name``.

    A component namespaces each ``child(...)`` under its own name:
    ``local_names("bucket", "policy", name="tfstate")`` is
    ``{"tfstate-bucket", "tfstate-policy"}``.
    """
    return frozenset(f"{name}-{kid}" for kid in children)


@cache
def _installed_mutability() -> dict[str, dict[str, Mutability]]:
    """The mutability of every type the installed provider plugins declare.

    Discovered as the CLI discovers them (``ATLANTIDE_NO_PLUGINS`` included), once
    per process. A plugin refused over its identity aborts, as it aborts a run.
    """
    found = discover()
    refused = [problem for problem in found.errors if problem.fatal]
    if refused:
        raise RegistryError(
            f"provider plugin {refused[0].name!r} could not be registered: {refused[0].detail}"
        )
    return type_mutability(found.types())


def _installed_mutability_copy() -> dict[str, dict[str, Mutability]]:
    """A private copy of :func:`_installed_mutability`'s cached table.

    Each :class:`Compiled` gets its own, so mutating one instance's table cannot
    leak into the cache or into every other instance.
    """
    return {type_name: dict(fields) for type_name, fields in _installed_mutability().items()}


@dataclass(frozen=True)
class Compiled:
    """One evaluation of a config: its IR, the Merkle hash of each node, and the
    per-type field mutability a run over it classifies changes by."""

    ir: IRGraph
    hashes: Mapping[str, str]
    mutability: TypeMutability = field(
        default_factory=_installed_mutability_copy, repr=False, compare=False
    )

    @classmethod
    def of(
        cls,
        build: Callable[[], object],
        *,
        stack: str = DEFAULT_STACK,
        region: str,
        name_prefix: str | None = DEFAULT_NAME_PREFIX,
        tags: dict[str, str] | None = None,
        types: Mapping[str, type[Resource]] | None = None,
    ) -> Compiled:
        """Call ``build`` inside a stack, then lower and hash what it declared.

        The field mutability comes from every installed provider plugin, as the
        engine's does; ``types`` replaces that set, e.g. for a provider under
        development that is not installed. Every node's type needs an entry: one
        without would classify every changed field as mutable.
        """
        with (
            collecting() as registry,
            Stack(stack, region=region, name_prefix=name_prefix, tags=tags),
        ):
            build()
        # No provider is built: nodes lower with an empty provider_version, which
        # is not part of the Merkle hash.
        compiled = raise_on_failure(compile_registry(registry, ProviderRegistry()))
        mutability = _installed_mutability_copy() if types is None else type_mutability(types)
        unknown = sorted({node.type for node in compiled.ir.nodes} - set(mutability))
        if unknown:
            raise RegistryError(
                f"no field mutability for {', '.join(map(repr, unknown))}: install the "
                f"provider plugin that declares it, or pass it in types="
            )
        return cls(compiled.ir, compiled.hashes, mutability)

    @classmethod
    def empty(cls) -> Compiled:
        """A config declaring nothing: planned over a prior config, a teardown.

        Its mutability table is empty: it declares no node to classify a change
        of, so it needs no plugin discovery.
        """
        return cls(IRGraph(nodes=()), {}, {})

    @property
    def bytes(self) -> bytes:
        """The canonical IR encoding: equal for equal configs."""
        return canonical_bytes(self.ir)

    @property
    def names(self) -> frozenset[str]:
        """The local name of every node (``"tfstate-bucket"``, not the full id)."""
        return frozenset(local_name_of(node.id) for node in self.ir.nodes)

    def __getitem__(self, local_name: str) -> IRNode:
        """The IR node with this local name."""
        return _named(self.ir.nodes, lambda node: node.id, local_name)

    def state(self) -> StateGraph:
        """The state a successful apply of this config would have committed.

        Provider outputs are empty: the diff never reads them.
        """
        return StateGraph(
            {node.id: _committed(node, self.hashes[node.id]) for node in self.ir.nodes}
        )

    def against(self, prior: Compiled | Plan | StateGraph | None = None) -> Plan:
        """The plan a run of this config makes over ``prior``'s committed state.

        ``prior`` is a config (the state its apply committed), a :class:`Plan`
        (the state applying that plan commits, see :meth:`Plan.committed`), or a
        :class:`~atlantide.state.StateGraph` as is. It defaults to empty state: a
        first run. Classified by this config's mutability.
        """
        state = _state_of(prior)
        return Plan(
            diff(self.ir, self.hashes, state, self.mutability),
            protected_ids(state, self.ir),
            hashes=self.hashes,
            prior=state,
        )


def _state_of(prior: Compiled | Plan | StateGraph | None) -> StateGraph:
    if prior is None:
        return StateGraph()
    if isinstance(prior, StateGraph):
        return prior
    return prior.state() if isinstance(prior, Compiled) else prior.committed()


@dataclass(frozen=True)
class Plan:
    """The changeset of one run, and the ids its ``prevent_destroy`` guard protects."""

    changes: ChangeSet
    #: What the planner's guard protects: the flag the config sets for every node
    #: it declares, and the committed flag for a node it drops.
    protected: frozenset[str]
    #: The planned config's Merkle hashes, which :meth:`committed` writes.
    hashes: Mapping[str, str] = field(default_factory=dict, repr=False)
    #: The state planned over, which :meth:`committed` starts from.
    prior: StateGraph = field(default_factory=StateGraph, repr=False)

    @property
    def actions(self) -> dict[str, Action]:
        """The action per node, keyed by local name; unchanged nodes are ``NOOP``.

        ``KeyError`` when two nodes (in different stacks) share a local name, as
        :meth:`__getitem__` raises: one would otherwise overwrite the other.
        """
        actions: dict[str, Action] = {}
        for change in self.changes:
            local_name = local_name_of(change.node_id)
            if local_name in actions:
                raise KeyError(f"several nodes are named {local_name!r}")
            actions[local_name] = change.action
        return actions

    def __getitem__(self, local_name: str) -> Change:
        """The change for this local name: its action, ``changed_fields``, ``conditional``."""
        return _named(self.changes, lambda change: change.node_id, local_name)

    @property
    def state_only(self) -> frozenset[str]:
        """Local names whose apply rewrites only the state row: a ``prevent_destroy``
        change on an otherwise unchanged node, which calls no provider."""
        return frozenset(local_name_of(c.node_id) for c in self.changes if c.state_only)

    def approve(self) -> Result[ChangeSet, PreventDestroyError]:
        """The planner's ``prevent_destroy`` verdict on this changeset.

        ``Failure(PreventDestroyError)`` when it deletes or replaces a protected
        node, else ``Success`` of the changeset.
        """
        return check_prevent_destroy(self.changes, self.protected)

    def committed(self) -> StateGraph:
        """The state a successful apply of this plan would commit.

        A NOOP keeps its prior row (with the new flag, for a state-only change); a
        CREATE, UPDATE or REPLACE writes the planned node's row, as
        :meth:`Compiled.state` does; a DELETE drops the row. A conditional REPLACE
        writes the same row whether the apply confirms it or runs it as an
        update. Provider outputs are empty, as in :meth:`Compiled.state`.
        """
        rows = dict(self.prior.nodes)
        for change in self.changes:
            row = _row_after(change, rows.get(change.node_id), self.hashes)
            if row is None:
                rows.pop(change.node_id, None)
            else:
                rows[change.node_id] = row
        return StateGraph(rows)


def _named[T](items: Iterable[T], node_id: Callable[[T], str], local_name: str) -> T:
    """The one item whose node id has this local name, else ``KeyError``."""
    found = [item for item in items if local_name_of(node_id(item)) == local_name]
    if len(found) != 1:
        problem = "several nodes are" if found else "no node is"
        raise KeyError(f"{problem} named {local_name!r}")
    return found[0]


def _row_after(
    change: Change, prior: StateNode | None, hashes: Mapping[str, str]
) -> StateNode | None:
    """The node's state row once ``change`` is applied over ``prior``, or ``None`` if none."""
    if change.action is Action.DELETE:
        return None
    if change.action is Action.NOOP:
        if prior is not None and change.state_only and change.desired is not None:
            return replace(prior, prevent_destroy=change.desired.prevent_destroy)
        return prior  # nothing written
    assert change.desired is not None  # every non-DELETE action has a desired node
    return _committed(change.desired, hashes[change.node_id])


def _committed(node: IRNode, input_hash: str) -> StateNode:
    """The state row an apply of ``node`` writes, minus provider outputs."""
    return StateNode(
        id=node.id,
        type=node.type,
        provider=node.provider,
        provider_version=node.provider_version,
        input_hash=input_hash,
        properties=dict(node.properties),
        dependencies=node.dependencies,
        depends_on=node.depends_on,
        prevent_destroy=node.prevent_destroy,
    )
