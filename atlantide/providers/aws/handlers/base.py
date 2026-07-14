"""The AWS handler contract and helpers shared by every service module.

One handler per resource type owns its boto3 service and its CRUD logic;
``AwsProvider`` dispatches over :data:`~atlantide.providers.aws.handlers.HANDLERS`.
Handlers are synchronous (boto3 is sync) and run in a worker thread. ``client``
is typed ``Any`` to avoid a dependency on per-service type stubs.
"""

from __future__ import annotations

import contextlib
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any, ClassVar, Generic, TypeVar

from botocore.exceptions import ClientError

from atlantide.core import Resource

R = TypeVar("R", bound=Resource)


def tag_list(tags: dict[str, str]) -> list[dict[str, str]]:
    """AWS ``[{"Key": k, "Value": v}]`` tag shape, deterministically ordered."""
    return [{"Key": k, "Value": v} for k, v in sorted(tags.items())]


def known_id(res: Resource, field: str) -> str | None:
    """The resource's real id when state restored it onto ``field``.

    A computed field with no value reads back as a ``Ref`` (see
    ``Resource.__getattribute__``); only a concrete non-empty string is a usable
    id. Update and delete act on that id rather than re-discovering the resource
    by attribute.
    """
    value = getattr(res, field, None)
    return value if isinstance(value, str) and value else None


#: Error codes meaning "the resource is already gone" — safe to ignore on delete.
_MISSING_CODES = frozenset({
    "NoSuchEntity", "NoSuchEntityException", "NoSuchBucket",
    "ResourceNotFoundException", "ResourceNotFound", "404",
    "NoSuchOriginAccessControl", "NoSuchDistribution", "NoSuchHostedZone",
})


@contextlib.contextmanager
def ignore_missing() -> Iterator[None]:
    """Swallow a delete's not-found error so destroy is idempotent.

    A 'creating' state row may point at a resource whose create never reached AWS
    or was already removed; deleting it is then a no-op rather than a hard error.
    """
    try:
        yield
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in _MISSING_CODES:
            raise


class AwsHandler(ABC, Generic[R]):
    """CRUD for one AWS resource type ``R`` over one boto3 service.

    Generic in ``R`` so each handler's methods receive its concrete resource
    type; the dispatcher looks the handler up by ``type_name`` and only hands it
    a matching resource, so no runtime ``isinstance`` guard is needed.
    """

    service: ClassVar[str]
    resource_type: ClassVar[type[Resource]]

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
