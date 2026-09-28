"""The AWS handler contract and helpers shared by every service module.

One handler per resource type owns its boto3 service and its CRUD logic;
``AwsProvider`` dispatches over :data:`~atlantide.providers.aws.handlers.HANDLERS`.
Handlers are synchronous (boto3 is sync) and run in a worker thread; ``client``
is typed :data:`Client`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from atlantide.core import Resource

# Re-exported so a handler imports the contract and its common helpers from one module.
from atlantide.providers.aws.handlers.faults import (
    create_or_adopt,
    error_code,
    ignore_missing,
    is_missing,
)
from atlantide.providers.aws.handlers.tags import (
    stale_tag_keys,
    sync_tags,
    tag_list,
    tags_from_list,
)

__all__ = [
    "AwsHandler",
    "Client",
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

#: A boto3 service client. boto3 builds clients at runtime and only
#: ``boto3-stubs[s3]`` is installed, so the alias is ``Any`` and names intent only.
Client = Any


def known_id(res: Resource, field: str) -> str | None:
    """The resource's real id when state restored it onto ``field``.

    A computed field with no value reads back as a ``Ref`` (see
    ``Resource.__getattribute__``); only a concrete non-empty string is a usable
    id. Update and delete act on that id rather than re-discovering the resource
    by attribute.
    """
    value = getattr(res, field, None)
    return value if isinstance(value, str) and value else None


class AwsHandler[R: Resource](ABC):
    """CRUD for one AWS resource type ``R`` over one boto3 service.

    Generic in ``R`` so each handler's methods receive its concrete resource
    type; the dispatcher looks the handler up by ``type_name`` and only hands it
    a matching resource, so no runtime ``isinstance`` guard is needed.
    """

    service: ClassVar[str]
    resource_type: ClassVar[type[Resource]]

    #: The computed field holding the provider-assigned id, for types AWS locates
    #: by an opaque id rather than a name (an ACM certificate's ``arn``, a VPC's
    #: ``vpc_id``). ``None`` means ``read`` finds the resource from its declared
    #: attributes and needs nothing restored.
    #:
    #: Declared rather than derived because it is an *input* to ``read``. Handlers
    #: pass it to ``known_id`` instead of a literal field name;
    #: ``tests/providers/aws/test_identity_fields.py`` checks that.
    identity_field: ClassVar[str | None] = None

    def region(self, res: R) -> str | None:
        """Client region; ``None`` uses the provider default (global services)."""
        return getattr(res, "region", None)

    def alias(self, res: R) -> str | None:
        """Credential/endpoint profile to use; ``None`` is the default session."""
        return getattr(res, "provider_alias", None)

    @abstractmethod
    def create(self, client: Client, res: R) -> dict[str, Any]: ...

    @abstractmethod
    def read(self, client: Client, res: R) -> dict[str, Any] | None: ...

    @abstractmethod
    def update(self, client: Client, prior: dict[str, Any], res: R) -> dict[str, Any]: ...

    @abstractmethod
    def delete(self, client: Client, res: R) -> None: ...
