"""AWS error classification and the adopt-on-conflict create helper.

The single definition of which error codes mean absence or already-exists.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from typing import Any

from botocore.exceptions import ClientError

from atlantide.core import Resource
from atlantide.core.errors import ProviderError
from atlantide.util.aws import error_code as error_code

#: Error codes meaning the resource does not exist; ignored on delete.
_MISSING_CODES = frozenset(
    {
        "NoSuchEntity",
        "NoSuchEntityException",
        "NoSuchBucket",
        "ResourceNotFoundException",
        "ResourceNotFound",
        "404",
        "NoSuchOriginAccessControl",
        "NoSuchDistribution",
        "NoSuchHostedZone",
        "NoSuchBucketPolicy",
        "NoSuchTagSet",
        "NoSuchCertificate",
    }
)

#: Some services use a per-type absence code: EC2 answers ``InvalidVpcID.NotFound``,
#: ``InvalidSubnetID.NotFound``, ``NatGatewayNotFound``, and so on. Matching the
#: suffix covers new types without enumerating them.
_MISSING_SUFFIX = "NotFound"


def is_missing(exc: ClientError) -> bool:
    """True only for "this resource does not exist".

    Denied, throttled and 5xx errors are not absence (``head_bucket`` answers 403
    for a bucket the caller may not see). Refresh maps a ``None`` read to MISSING
    and ``--write`` deletes the state row.
    """
    code = error_code(exc)
    return code in _MISSING_CODES or code.endswith(_MISSING_SUFFIX)


@contextlib.contextmanager
def ignore_missing() -> Iterator[None]:
    """Swallow a delete's not-found error so destroy is idempotent.

    A 'creating' state row may point at a resource whose create never reached AWS
    or that is already removed; deleting it is then a no-op.
    """
    try:
        yield
    except ClientError as exc:
        if not is_missing(exc):
            raise


#: Error codes meaning a resource with this name already exists.
_EXISTS_CODES = frozenset(
    {
        "EntityAlreadyExists",
        "EntityAlreadyExistsException",
        "ResourceConflictException",
        # DynamoDB also answers this for a table still DELETING; its delete waits
        # for the table to be gone so a replace never adopts the dying table.
        "ResourceInUseException",
        "BucketAlreadyOwnedByYou",
        "HostedZoneAlreadyExists",
        "QueueAlreadyExists",
        "TopicAlreadyExists",
        "ResourceAlreadyExistsException",
        "DistributionAlreadyExists",
        "OriginAccessControlAlreadyExists",
    }
)


def create_or_adopt(
    create: Callable[[], dict[str, Any]],
    read: Callable[[], dict[str, Any] | None],
) -> dict[str, Any]:
    """Run ``create``; if the resource already exists, adopt it via ``read``.

    A create re-runs whenever its state row never reached ``created``: the process
    was killed between the AWS call and the persist, or a failed sibling node
    cancelled the task. Adoption is keyed on the name ``read`` uses, so it
    resolves only to the resource this node declares.
    """
    try:
        return create()
    except ClientError as exc:
        if error_code(exc) not in _EXISTS_CODES:
            raise
        existing = read()
        if existing is None:  # vanished between the conflict and the read
            raise
        return existing


def absent_ok[T](call: Callable[[], T], *, default: T | None = None) -> T | None:
    """Run a read, mapping "this resource does not exist" to ``default``.

    The read-side counterpart of :func:`ignore_missing`. Denied or throttled calls
    raise: reported as absence, ``refresh --write`` would drop the state row of a
    healthy resource.
    """
    try:
        return call()
    except ClientError as exc:
        if is_missing(exc):
            return default
        raise


def not_found(res: Resource, op: str, detail: str = "") -> ProviderError:
    """The uniform "resource not found" error for update paths."""
    suffix = f" {detail}" if detail else ""
    return ProviderError(
        f"{res.type_name()} not found{suffix}",
        op=op,
        resource_type=res.type_name(),
    )
