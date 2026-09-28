"""What each ``$ref`` field of a node resolved to when the node was last applied.

The IR, the Merkle hash and a state row's ``properties`` are symbolic: a field
wired to an upstream output holds its ``{"$ref": "id#attr"}`` marker, never the
value. The marker cannot say *which* upstream value the resource was given. An
upstream whose outputs move without its dependents being re-applied (an apply
that stopped between the two, or one narrowed by ``--target``) leaves the
dependent's row, marker for marker, equal to the config, while the resource
still holds the old value.

So every row the engine writes records, per ``$ref``-bearing property, a digest
of the value that property resolved to (:attr:`StateNode.ref_digests
<atlantide.state.StateNode.ref_digests>`), and both halves of a run compare
against it:

- the **plan** resolves the config's markers against the upstreams' *stored*
  outputs (:func:`consumed`) and hands the diff a :class:`Consumed` verdict per
  field. The diff trusts a verdict only for a field whose upstreams all keep
  their outputs this run; a moved value there is a known change;
- the **apply** re-diffs a conditional REPLACE against the record
  (:func:`matches`, via :func:`~atlantide.reconcile.classify.reclassify`) instead
  of against the run-start outputs, which an interrupted run has already moved.

A digest, not the value: a value may derive from a ``sensitive`` field, and the
row need only answer "is it still the same?". Two schemes, told apart by prefix
so a verdict never depends on today's field flags:

- ``sha256:<hex>``, for values from non-sensitive fields: unsalted, so every
  install, keyfile or not, reaches the same verdicts. Such a value is stored in
  the clear in the upstream's outputs anyway;
- ``salted:<hex>``, when the value derives from a ``sensitive`` field (the
  upstream output's, or the node's own): the per-install salted digest the
  secret rotation digests use (:meth:`SecretsRegistry.digest
  <atlantide.secrets.SecretsRegistry.digest>`), so a low-entropy secret cannot
  be recovered from state by brute force.

Neither is scoped by node id, so renaming a resource through ``aliases`` keeps
its record valid.

A row written before the record existed has no entry for its fields: the plan
reports them :attr:`Consumed.UNRECORDED` and the apply falls back to resolving
the stored row against the run-start outputs (the behaviour before the record).
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from typing import Any

from pydantic_core import PydanticSerializationError, to_jsonable_python

from atlantide.core._tree import tree_collect
from atlantide.core.errors import AtlantideError
from atlantide.core.fields import sensitive_fields
from atlantide.core.markers import collect_ref_targets, is_ref_marker, ref_from_marker
from atlantide.core.node_id import type_name_of
from atlantide.core.resource import Resource
from atlantide.ir.model import IRGraph, IRNode
from atlantide.reconcile.env import LiveOutputs
from atlantide.reconcile.resolve import needs_resolution, resolve_value, unseal_outputs
from atlantide.reconcile.upstream import Consumed
from atlantide.secrets import SecretsRegistry
from atlantide.state import NodeStatus, StateGraph, StateNode
from atlantide.util.jsonfmt import compact_json

__all__ = [
    "PLAIN_PREFIX",
    "SALTED_PREFIX",
    "Consumed",
    "consumed",
    "digest",
    "matches",
    "ref_digests",
]

#: Prefix of an unsalted digest (a value from non-sensitive fields only).
PLAIN_PREFIX = "sha256:"
#: Prefix of an install-salted digest (a value derived from a sensitive field).
SALTED_PREFIX = "salted:"

#: Domain separation for the unsalted digest: no other atlantide digest collides.
_PLAIN_DOMAIN = b"atlantide/ref-digest/v1\0"

#: Values that cannot be normalized to JSON, so cannot be digested. Never raised
#: for anything read back from state, which is JSON already.
_UNDIGESTIBLE = (PydanticSerializationError, TypeError, ValueError)

#: What resolving a ``$ref`` against stored outputs can raise: a missing output
#: (``KeyError``), a sealed value this install cannot open, a transform over
#: operands of the wrong shape.
_UNRESOLVABLE = (KeyError, IndexError, TypeError, ValueError, AtlantideError)


def _canonical(value: Any) -> str:
    """``value`` as the canonical JSON its stored form would have.

    Normalized the way the state codec persists it (tuples become lists, keys
    strings), so a value digested at apply from a provider's return and one read
    back from state digest identically.
    """
    return compact_json(to_jsonable_python(value), ascii=False)


def digest(field: str, value: Any, *, sensitive: bool, secrets: SecretsRegistry) -> str:
    """The recorded digest of ``value`` as field ``field`` consumed it.

    ``sensitive`` picks the install-salted scheme; see the module doc. Raises
    one of :data:`_UNDIGESTIBLE` for a value that has no JSON form.
    """
    canonical = _canonical(value)
    if sensitive:
        return SALTED_PREFIX + secrets.digest(f"$ref:{field}", canonical)
    return PLAIN_PREFIX + hashlib.sha256(_PLAIN_DOMAIN + canonical.encode("utf-8")).hexdigest()


def matches(field: str, value: Any, recorded: str, secrets: SecretsRegistry) -> bool | None:
    """Whether ``value`` is the one ``recorded`` digests, or ``None`` if it cannot say.

    The scheme is the recorded one, so the verdict does not depend on which
    fields are ``sensitive`` today. ``None`` for an unknown scheme or a value
    with no JSON form.
    """
    if recorded.startswith(SALTED_PREFIX):
        sensitive = True
    elif recorded.startswith(PLAIN_PREFIX):
        sensitive = False
    else:
        return None
    try:
        fresh = digest(field, value, sensitive=sensitive, secrets=secrets)
    except _UNDIGESTIBLE:
        return None
    # Constant-time: a salted digest is of a sensitive value.
    return hmac.compare_digest(fresh, recorded)


def _is_sensitive(value: Any, types: Mapping[str, type[Resource]], *, own: bool) -> bool:
    """Whether a ref-bearing value derives from a ``sensitive`` field.

    ``own``: the consuming field itself is sensitive. Otherwise any referenced
    upstream output declared ``sensitive`` (or of a type this build does not
    know, as :func:`~atlantide.reconcile.resolve.sensitive_output_names` treats
    it) makes the value sensitive.
    """
    if own:
        return True
    for marker in tree_collect(value, is_ref_marker, include_sets=False):
        ref = ref_from_marker(marker)
        cls = types.get(type_name_of(ref.node_id))
        if cls is None or ref.attr in sensitive_fields(cls):
            return True
    return False


def ref_digests(
    type_name: str,
    properties: Mapping[str, Any],
    outputs: LiveOutputs,
    *,
    types: Mapping[str, type[Resource]],
    secrets: SecretsRegistry,
) -> dict[str, str]:
    """The record for a row about to be written: a digest per ``$ref``-bearing property.

    ``properties`` are the row's (symbolic) properties and ``outputs`` the
    plaintext upstream outputs they were resolved against for the provider
    call, so each digest is of the value the resource was actually given. A
    property that does not resolve against ``outputs`` is left unrecorded,
    which later reads as "unknown", never as "moved".
    """
    cls = types.get(type_name)
    own_sensitive = frozenset(sensitive_fields(cls)) if cls is not None else frozenset()
    recorded: dict[str, str] = {}
    for name, value in properties.items():
        if not needs_resolution(value):
            continue
        try:
            resolved = resolve_value(value, outputs)
            recorded[name] = digest(
                name,
                resolved,
                sensitive=_is_sensitive(value, types, own=name in own_sensitive),
                secrets=secrets,
            )
        except (*_UNRESOLVABLE, *_UNDIGESTIBLE):
            continue
    return recorded


#: Distinguishes a property missing from a stored row from one holding ``None``.
_ABSENT = object()


def consumed(
    desired: IRGraph, prior: StateGraph, secrets: SecretsRegistry
) -> dict[str, dict[str, Consumed]]:
    """Per node, its ``$ref`` fields whose recorded value is not what they resolve to now.

    Resolved against the upstreams' *stored* outputs. Only fields the config did
    not change are examined (the diff already sees a changed marker), and only
    on confirmed rows (anything else is re-created). Whether the upstream is
    itself changing this run is the diff's call: it keeps a verdict only for a
    field whose upstreams all keep their outputs.
    """
    check = _RecordCheck(prior, secrets)
    verdicts: dict[str, dict[str, Consumed]] = {}
    for node in desired.nodes:
        have = prior.get(node.id)
        if have is None or have.status != NodeStatus.CREATED:
            continue
        if fields := check.node(node, have):
            verdicts[node.id] = fields
    return verdicts


class _RecordCheck:
    """One :func:`consumed` pass: stored rows' records against their upstreams' stored outputs.

    Unsealing is lazy and per upstream, and an upstream whose outputs this
    install cannot open (a foreign keyfile) yields no verdict, which is what the
    plan had before the record: no false "moved".
    """

    def __init__(self, prior: StateGraph, secrets: SecretsRegistry) -> None:
        self._prior = prior
        self._secrets = secrets
        self._opened: dict[str, dict[str, Any] | None] = {}

    def node(self, node: IRNode, have: StateNode) -> dict[str, Consumed]:
        """The verdict of each ``$ref`` field of ``node`` the config left unchanged."""
        ignored = set(node.ignore_changes)
        fields: dict[str, Consumed] = {}
        for name, value in node.properties.items():
            if (
                name in ignored
                or not needs_resolution(value)
                or have.properties.get(name, _ABSENT) != value
            ):
                continue
            verdict = self._field(name, value, have.ref_digests.get(name))
            if verdict is not None:
                fields[name] = verdict
        return fields

    def _field(self, name: str, value: Any, recorded: str | None) -> Consumed | None:
        """One field's verdict; ``None`` when it did not move or cannot be resolved."""
        if recorded is None:
            return Consumed.UNRECORDED
        upstream = {target: self._outputs_of(target) for target in collect_ref_targets(value)}
        available = {k: v for k, v in upstream.items() if v is not None}
        if len(available) != len(upstream):
            return None
        try:
            resolved = resolve_value(value, available)
        except _UNRESOLVABLE:
            return None
        # Uncaught: a salted record with no keyfile to check it raises the
        # SecretsError that fails the plan.
        if matches(name, resolved, recorded, self._secrets) is False:
            return Consumed.MOVED
        return None

    def _outputs_of(self, node_id: str) -> dict[str, Any] | None:
        """``node_id``'s stored outputs, unsealed; ``None`` if unconfirmed or unopenable."""
        if node_id not in self._opened:
            row = self._prior.get(node_id)
            try:
                self._opened[node_id] = (
                    unseal_outputs(row.outputs, self._secrets)
                    if row is not None and row.status == NodeStatus.CREATED
                    else None
                )
            except AtlantideError:
                self._opened[node_id] = None
        return self._opened[node_id]
