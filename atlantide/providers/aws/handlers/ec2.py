"""The shared base for every EC2-backed handler.

Not networking-specific: any resource EC2 locates by id uses it. The EC2 API has
no name-based ``get`` and attributes are not unique, so identity is carried on a tag.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any, ClassVar, override

from botocore.exceptions import ClientError

from atlantide.providers.aws.handlers.base import (
    AwsHandler,
    Client,
    ignore_missing,
    is_missing,
    known_id,
    sync_tags,
    tag_list,
    tags_from_list,
)
from atlantide.providers.aws.handlers.faults import not_found
from atlantide.providers.aws.resources import Ec2Resource

#: Tag key naming the owning node; the identity a create adopts on
#: (see :class:`Ec2Handler`).
MANAGED_TAG = "atlantide:node"


def tag_filter(node_id: str) -> list[dict[str, Any]]:
    """An EC2 ``Filters`` entry matching only what this node created."""
    return [{"Name": f"tag:{MANAGED_TAG}", "Values": [node_id]}]


def tag_spec(resource_type: str, node_id: str) -> list[dict[str, Any]]:
    """``TagSpecifications`` stamping :data:`MANAGED_TAG` atomically with a create.

    A crash or transport-level retry between an untagged create and :func:`ec2_tag`
    leaves a resource ``_find_tagged`` cannot see, so the re-run provisions a
    duplicate. User tags are applied by the post-create sync.
    """
    return [{"ResourceType": resource_type, "Tags": [{"Key": MANAGED_TAG, "Value": node_id}]}]


def ec2_tag(client: Client, resource_id: str, tags: dict[str, str]) -> None:
    if tags:
        client.create_tags(Resources=[resource_id], Tags=tag_list(tags))


class Ec2Handler[E: Ec2Resource](AwsHandler[E]):
    """CRUD for an EC2 resource located by its id, with attribute lookup as fallback.

    EC2 has no name-based ``get``, so a resource with no known id is discovered by
    its attributes (``_find``). EC2 attributes are not unique (an account may hold
    several ``10.0.0.0/16`` VPCs) and an out-of-band edit stops a resource matching
    config, so every operation prefers the id persisted in state or this node's
    :data:`MANAGED_TAG`:

    * ``create`` adopts by the node tag only; an attribute match could adopt an
      unrelated resource.
    * ``read`` and ``update`` use the state id first and ``_find`` only when none
      is known. With two matching VPCs an attribute read returns whichever the API
      lists first; after a console CIDR edit it matches nothing and reports
      spurious drift.
    * ``delete`` never consults ``_find``: a state row whose create never reached
      AWS would attribute-match a pre-existing unmanaged resource and delete it.

    A subclass supplies ``identity_field``, the describe wiring below, ``_create``,
    ``_find`` and ``_delete``, and may override ``_observed``. Tags are applied on
    create and update.
    """

    service = "ec2"
    identity_field: ClassVar[str]

    #: Describe wiring: the boto3 list call, the plural envelope key, the id key
    #: inside one item, and the ``<X>Ids`` kwarg for by-id reads.
    describe_call: ClassVar[str]
    list_key: ClassVar[str]
    id_key: ClassVar[str]
    ids_kwarg: ClassVar[str]
    #: The filter kwarg; EC2 spells it ``Filter`` for NAT gateways only.
    filters_kwarg: ClassVar[str] = "Filters"

    def _known_id(self, res: E) -> str | None:
        """This resource's real id from state, or None when not yet known."""
        return known_id(res, self.identity_field)

    @abstractmethod
    def _create(self, client: Client, res: E) -> str:
        """Create the resource and return its id."""

    @abstractmethod
    def _find(self, client: Client, res: E) -> str | None:
        """Resolve the resource's id from its attributes, or None if it is absent."""

    def _items(self, client: Client, **kwargs: Any) -> list[dict[str, Any]]:
        """The live items one describe call returns (see :meth:`_is_live`)."""
        items = getattr(client, self.describe_call)(**kwargs).get(self.list_key, [])
        return [item for item in items if self._is_live(item)]

    @staticmethod
    def _is_live(item: dict[str, Any]) -> bool:
        """Whether an item counts as existing (NAT gateways linger after delete)."""
        return True

    def _first_id(self, client: Client, **kwargs: Any) -> str | None:
        """The first matching item's id, or ``None``."""
        items = self._items(client, **kwargs)
        return str(items[0][self.id_key]) if items else None

    def _describe(self, client: Client, resource_id: str) -> dict[str, Any] | None:
        """The live resource with this exact id, or ``None`` if it no longer exists."""
        try:
            items = self._items(client, **{self.ids_kwarg: [resource_id]})
        except ClientError as exc:
            if is_missing(exc):
                return None
            raise
        return items[0] if items else None

    def _observed(self, live: dict[str, Any]) -> dict[str, Any]:
        """Fields beyond the id that ``read`` reports, drawn from ``_describe``.

        Refresh detects drift only on the fields returned here; an unreported field
        is unchecked. The default is empty: most EC2 resources have no mutable
        attribute beyond their tags.

        Takes only the live payload so a hook cannot substitute the desired value
        for a key AWS omits. Omit the key instead; :mod:`atlantide.reconcile.refresh`
        reports it as unchecked.
        """
        return {}

    def _find_tagged(self, client: Client, node_id: str) -> str | None:
        """The id of the resource this node created, found by :data:`MANAGED_TAG`.

        Unlike ``_find`` this cannot match a resource owned by anything else, so
        create may adopt its result. Returns ``None`` for an untagged resource.
        """
        return self._first_id(client, **{self.filters_kwarg: tag_filter(node_id)})

    @abstractmethod
    def _delete(self, client: Client, resource_id: str) -> None:
        """Delete the resource by id."""

    def _after_create(self, client: Client, resource_id: str, res: E) -> None:
        """Post-create configuration, run *after* the identity tag is applied.

        Follow-up calls (security-group rules, subnet attributes, gateway
        attachment) belong here rather than in ``_create``: a failure before the
        tag leaves a resource ``_find_tagged`` cannot see, and the re-run's create
        collides with it. After the tag, a failed follow-up re-runs via adoption.
        Must be idempotent.
        """

    @override
    def create(self, client: Client, res: E) -> dict[str, Any]:
        # Adopt this node's earlier create (see `create_or_adopt` for when a create
        # re-runs). Keyed on the node tag, never `_find`.
        resource_id = self._find_tagged(client, res.node_id) or self._create(client, res)
        ec2_tag(client, resource_id, {**res.tags, MANAGED_TAG: res.node_id})
        self._after_create(client, resource_id, res)
        return {self.identity_field: resource_id}

    @override
    def read(self, client: Client, res: E) -> dict[str, Any] | None:
        resource_id = self._known_id(res) or self._find(client, res)
        if resource_id is None:
            return None
        live = self._describe(client, resource_id)
        if live is None:
            return None
        observed = {self.identity_field: resource_id, **self._observed(live)}
        # Tags are synced, so they are observed to surface console edits as drift.
        if "Tags" in live:
            tags = tags_from_list(live["Tags"])
            tags.pop(MANAGED_TAG, None)  # not declared in config
            observed["tags"] = tags
        return observed

    @override
    def update(self, client: Client, prior: dict[str, Any], res: E) -> dict[str, Any]:
        resource_id = (
            prior.get(self.identity_field) or self._known_id(res) or self._find(client, res)
        )
        if resource_id is None:
            raise not_found(res, "update", "by state id or attributes")
        # EC2 tagging is additive, so a tag removed from config must be deleted
        # explicitly. Including the managed tag makes an untagged resource adoptable.
        sync_tags(
            {**res.tags, MANAGED_TAG: res.node_id},
            live=lambda: self._live_tags(client, resource_id),
            untag=lambda stale, _: client.delete_tags(
                Resources=[resource_id], Tags=[{"Key": key} for key in stale]
            ),
            tag=lambda tags: ec2_tag(client, resource_id, tags),
        )
        return {self.identity_field: resource_id}

    def _live_tags(self, client: Client, resource_id: str) -> dict[str, str]:
        live = self._describe(client, resource_id) or {}
        return tags_from_list(live.get("Tags", []))

    @override
    def delete(self, client: Client, res: E) -> None:
        # Only the state id or the node tag, never `_find`. Ignoring not-found keeps
        # destroy idempotent.
        resource_id = self._known_id(res) or self._find_tagged(client, res.node_id)
        if resource_id is not None:
            with ignore_missing():
                self._delete(client, resource_id)
