"""The shared base for every EC2-backed handler.

Split out of ``networking.py`` because it is not about networking: subnets, route
tables and anything else EC2 locates by id share it, and a future non-network EC2
resource would too. What it encodes is the awkward part of the EC2 API — there is
no name-based ``get``, and attributes are not unique — so identity has to be
carried on a tag.
"""

from __future__ import annotations

from abc import abstractmethod
from typing import Any, ClassVar, TypeVar

from botocore.exceptions import ClientError
from typing_extensions import override

from atlantide.providers.aws.handlers.base import (
    AwsHandler,
    ignore_missing,
    is_missing,
    known_id,
    sync_tags,
    tag_list,
    tags_from_list,
)
from atlantide.providers.aws.handlers.faults import not_found
from atlantide.providers.aws.resources import Ec2Resource

E = TypeVar("E", bound=Ec2Resource)

#: Tags a resource with the node that owns it. EC2 has no name-based `get` and
#: its attributes are not unique, so this is the identity a create adopts on.
MANAGED_TAG = "atlantide:node"


def tag_filter(node_id: str) -> list[dict[str, Any]]:
    """An EC2 ``Filters`` entry matching only what this node created."""
    return [{"Name": f"tag:{MANAGED_TAG}", "Values": [node_id]}]


def tag_spec(resource_type: str, node_id: str) -> list[dict[str, Any]]:
    """``TagSpecifications`` stamping :data:`MANAGED_TAG` atomically with a create.

    Tagging only after ``_create`` returns leaves a window: a crash — or a
    transport-level retry of the whole create — between the create call and
    :func:`ec2_tag` produces a resource ``_find_tagged`` cannot see, so the
    re-run provisions a duplicate. Every ``_create`` passes this so the identity
    tag is part of the create itself; user tags still ride the post-create sync.
    """
    return [{"ResourceType": resource_type, "Tags": [{"Key": MANAGED_TAG, "Value": node_id}]}]


def ec2_tag(client: Any, resource_id: str, tags: dict[str, str]) -> None:
    if tags:
        client.create_tags(Resources=[resource_id], Tags=tag_list(tags))


class Ec2Handler(AwsHandler[E]):
    """CRUD for an EC2 resource located by its id, with attribute lookup as fallback.

    EC2 has no name-based ``get``, so a resource with no known id is discovered by
    its attributes (``_find``). Every operation prefers the id persisted in state;
    attribute lookup is a fallback only, since attributes such as a VPC's CIDR are
    not unique. A subclass supplies the id field name (``identity_field``), the
    declarative describe wiring below, ``_create``, ``_find`` and ``_delete``, and
    may override ``_observed`` to report more than the id. Tags are (re)applied on
    create and update.
    """

    service = "ec2"
    identity_field: ClassVar[str]

    #: Declarative describe wiring: the boto3 list call, the plural envelope key,
    #: the id key inside one item, and the ``<X>Ids`` kwarg for by-id reads. These
    #: four names are the only thing that varied across seven hand-written
    #: ``_describe``/``_find_tagged`` pairs.
    describe_call: ClassVar[str]
    list_key: ClassVar[str]
    id_key: ClassVar[str]
    ids_kwarg: ClassVar[str]
    #: The filter kwarg — EC2 spells it ``Filter`` for NAT gateways alone.
    filters_kwarg: ClassVar[str] = "Filters"

    def _known_id(self, res: E) -> str | None:
        """This resource's real id from state, or None when not yet known."""
        return known_id(res, self.identity_field)

    @abstractmethod
    def _create(self, client: Any, res: E) -> str:
        """Create the resource and return its id."""

    @abstractmethod
    def _find(self, client: Any, res: E) -> str | None:
        """Resolve the resource's id from its attributes, or None if it is absent."""

    def _items(self, client: Any, **kwargs: Any) -> list[dict[str, Any]]:
        """The live items one describe call returns (see :meth:`_is_live`)."""
        items = getattr(client, self.describe_call)(**kwargs).get(self.list_key, [])
        return [item for item in items if self._is_live(item)]

    @staticmethod
    def _is_live(item: dict[str, Any]) -> bool:
        """Whether an item counts as existing — NAT gateways linger after delete."""
        return True

    def _first_id(self, client: Any, **kwargs: Any) -> str | None:
        """The first matching item's id, or ``None`` — the common lookup tail."""
        items = self._items(client, **kwargs)
        return str(items[0][self.id_key]) if items else None

    def _describe(self, client: Any, resource_id: str) -> dict[str, Any] | None:
        """The live resource with this exact id, or ``None`` if it no longer exists.

        A describe by id answers "does this exact resource still exist", which a
        describe by attribute cannot: attributes are not unique, and a resource
        edited out of band no longer matches the ones config declares.
        """
        try:
            items = self._items(client, **{self.ids_kwarg: [resource_id]})
        except ClientError as exc:
            if is_missing(exc):
                return None
            raise
        return items[0] if items else None

    def _observed(self, live: dict[str, Any]) -> dict[str, Any]:
        """Fields beyond the id that ``read`` reports, drawn from ``_describe``.

        Whatever a handler returns here is what refresh can detect drift *on*: a
        field nobody reports is not "in sync", it is unchecked. The default is
        empty because most EC2 resources have no mutable attribute worth watching
        beyond their tags.

        Takes only the live payload — deliberately not the resource. A hook with
        the desired state in hand can fall back to it when AWS omits a key, and
        that reports what config *said* as though it had been *observed*, which is
        the one way the coverage machinery in :mod:`atlantide.reconcile.refresh`
        can be fooled. Omit a key instead: unchecked is a truth it can render.
        """
        return {}

    def _find_tagged(self, client: Any, node_id: str) -> str | None:
        """The id of the resource this node created, found by :data:`MANAGED_TAG`.

        Unlike ``_find`` this cannot match a resource owned by anything else,
        which is what makes it safe to adopt on create. Returns ``None`` for a
        resource created before the tag existed, or one whose create was
        interrupted before tagging; both fall through to a fresh create.
        """
        return self._first_id(client, **{self.filters_kwarg: tag_filter(node_id)})

    @abstractmethod
    def _delete(self, client: Any, resource_id: str) -> None:
        """Delete the resource by id."""

    def _after_create(self, client: Any, resource_id: str, res: E) -> None:
        """Post-create configuration, run *after* the identity tag is applied.

        A subclass with follow-up calls (security-group rules, subnet
        attributes, gateway attachment) puts them here rather than in
        ``_create``: a failure between the bare create and the tag would leave a
        resource ``_find_tagged`` cannot see, so the re-run's create collides
        with it and the apply is wedged until manual cleanup. After the tag, a
        failed follow-up re-runs via adoption. Must be idempotent.
        """

    @override
    def create(self, client: Any, res: E) -> dict[str, Any]:
        # Adopt this node's own earlier create rather than provisioning a second
        # resource; see `create_or_adopt` for when a create is re-run.
        #
        # Keyed on the node tag, not `_find`: EC2 attributes are not unique (an
        # account may hold several 10.0.0.0/16 VPCs), so an attribute match could
        # adopt an unrelated resource.
        resource_id = self._find_tagged(client, res.node_id) or self._create(client, res)
        ec2_tag(client, resource_id, {**res.tags, MANAGED_TAG: res.node_id})
        self._after_create(client, resource_id, res)
        return {self.identity_field: resource_id}

    @override
    def read(self, client: Any, res: E) -> dict[str, Any] | None:
        # The id from state first, exactly as update and delete do. Reading by
        # attribute instead is wrong in both directions: an account holding two
        # 10.0.0.0/16 VPCs answers with whichever the API returns first, and a VPC
        # whose CIDR was edited in the console matches nothing and reads as gone.
        # Neither is visible in the result — one silently tracks the wrong
        # resource, the other reports drift that is not there.
        resource_id = self._known_id(res) or self._find(client, res)
        if resource_id is None:
            return None
        live = self._describe(client, resource_id)
        if live is None:  # the id is known but the resource is gone
            return None
        observed = {self.identity_field: resource_id, **self._observed(live)}
        # Tags are mutable and synced, so they are observed too — otherwise a tag
        # deleted from config (or edited in the console) is invisible forever.
        if "Tags" in live:
            tags = tags_from_list(live["Tags"])
            tags.pop(MANAGED_TAG, None)  # ours, not config's
            observed["tags"] = tags
        return observed

    @override
    def update(self, client: Any, prior: dict[str, Any], res: E) -> dict[str, Any]:
        # Act on the id persisted in state; attribute lookup is a fallback only.
        resource_id = (
            prior.get(self.identity_field) or self._known_id(res) or self._find(client, res)
        )
        if resource_id is None:  # update runs only on an existing resource
            raise not_found(res, "update", "by state id or attributes")
        # Synced, not merely restamped: EC2 tagging is additive, so a tag removed
        # from config has to be deleted explicitly or it stays forever. The
        # managed tag rides along so a resource created before it becomes
        # adoptable.
        sync_tags(
            {**res.tags, MANAGED_TAG: res.node_id},
            live=lambda: self._live_tags(client, resource_id),
            untag=lambda stale, _: client.delete_tags(
                Resources=[resource_id], Tags=[{"Key": key} for key in stale]
            ),
            tag=lambda tags: ec2_tag(client, resource_id, tags),
        )
        return {self.identity_field: resource_id}

    def _live_tags(self, client: Any, resource_id: str) -> dict[str, str]:
        live = self._describe(client, resource_id) or {}
        return tags_from_list(live.get("Tags", []))

    @override
    def delete(self, client: Any, res: E) -> None:
        # Only the id from state, or the node's own tag. The attribute `_find` is
        # deliberately not consulted: attributes are not unique, and a state row
        # whose create never reached AWS would attribute-match a pre-existing
        # unmanaged resource (any VPC with the same CIDR) and destroy it — the
        # create path refuses attribute matching for exactly this reason. The
        # not-found tolerance makes destroy idempotent, per the base contract.
        resource_id = self._known_id(res) or self._find_tagged(client, res.node_id)
        if resource_id is not None:
            with ignore_missing():
                self._delete(client, resource_id)
