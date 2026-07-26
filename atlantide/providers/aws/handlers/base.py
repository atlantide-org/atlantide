"""The AWS handler contract and helpers shared by every service module.

One handler per resource type owns its boto3 service and its CRUD logic;
``AwsProvider`` dispatches over :data:`~atlantide.providers.aws.handlers.HANDLERS`.
Handlers are synchronous (boto3 is sync) and run in a worker thread. ``client``
is typed ``Any`` to avoid a dependency on per-service type stubs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Generic, TypeVar

from atlantide.core import Resource

R = TypeVar("R", bound=Resource)

# Split by concern; re-exported here because every handler already imports
# these names from base and the split should not ripple through them.
from atlantide.providers.aws.handlers.faults import (  # noqa: E402
    create_or_adopt,
    error_code,
    ignore_missing,
    is_missing,
)
from atlantide.providers.aws.handlers.tags import (  # noqa: E402
    stale_tag_keys,
    sync_tags,
    tag_list,
    tags_from_list,
)

__all__ = [
    "AwsHandler",
    "create_or_adopt",
    "error_code",
    "ignore_missing",
    "is_missing",
    "known_id",
    "stale_tag_keys",
    "sync_tags",
    "tag_list",
    "tags_from_list",
]


def known_id(res: Resource, field: str) -> str | None:
    """The resource's real id when state restored it onto ``field``.

    A computed field with no value reads back as a ``Ref`` (see
    ``Resource.__getattribute__``); only a concrete non-empty string is a usable
    id. Update and delete act on that id rather than re-discovering the resource
    by attribute.
    """
    value = getattr(res, field, None)
    return value if isinstance(value, str) and value else None


class AwsHandler(ABC, Generic[R]):
    """CRUD for one AWS resource type ``R`` over one boto3 service.

    Generic in ``R`` so each handler's methods receive its concrete resource
    type; the dispatcher looks the handler up by ``type_name`` and only hands it
    a matching resource, so no runtime ``isinstance`` guard is needed.
    """

    service: ClassVar[str]
    resource_type: ClassVar[type[Resource]]

    #: The computed field holding this resource's provider-assigned id, for the
    #: types AWS locates by an opaque id rather than by a name — an ACM
    #: certificate's ``arn``, a VPC's ``vpc_id``. ``None`` means ``read`` finds
    #: the resource from its declared attributes and needs nothing restored.
    #:
    #: Declared rather than derived because it is an *input* to ``read``: nothing
    #: about a call tells you that ACM keys on an arn and EC2 on a vpc id. (The
    #: opposite case — which fields a read *observed* — is derivable from its
    #: return value, and :mod:`atlantide.reconcile.refresh` deliberately derives
    #: it rather than declaring it.) The names here were already written down as
    #: ``known_id(res, "arn")`` literals; this collects them in one place, and
    #: ``tests/providers/test_identity_fields.py`` holds them to the handler.
    identity_field: ClassVar[str | None] = None

    def region(self, res: R) -> str | None:
        """Client region; ``None`` uses the provider default (global services)."""
        return getattr(res, "region", None)

    def alias(self, res: R) -> str | None:
        """Credential/endpoint profile to use; ``None`` is the default session."""
        return getattr(res, "provider_alias", None)

    @abstractmethod
    def create(self, client: Any, res: R) -> dict[str, Any]: ...

    @abstractmethod
    def read(self, client: Any, res: R) -> dict[str, Any] | None: ...

    @abstractmethod
    def update(self, client: Any, prior: dict[str, Any], res: R) -> dict[str, Any]: ...

    @abstractmethod
    def delete(self, client: Any, res: R) -> None: ...
