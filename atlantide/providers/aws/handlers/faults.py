"""AWS error classification and the adopt-on-conflict create helper.

One home for "is this error absence / already-exists", so handlers cannot
drift on which codes mean what.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from typing import Any, TypeVar

from botocore.exceptions import ClientError

from atlantide.core import Resource
from atlantide.core.errors import ProviderError

_T = TypeVar("_T")

#: Error codes meaning "the resource is already gone" — safe to ignore on delete.
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

#: Services that spell absence per resource type rather than with one shared
#: code — EC2 answers ``InvalidVpcID.NotFound``, ``InvalidSubnetID.NotFound``,
#: ``NatGatewayNotFound``, and a new one for every type it grows. Enumerating
#: those would mean a set that has to be extended in step with AWS, where each
#: omission is silent: a ``read`` raises instead of reporting absence, and only
#: in production. The suffix is the general form of the same fact.
_MISSING_SUFFIX = "NotFound"


def error_code(exc: ClientError) -> str:
    """The AWS error code, or ``""`` when the response carries none."""
    code = exc.response.get("Error", {}).get("Code")
    return str(code) if code is not None else ""


def is_missing(exc: ClientError) -> bool:
    """True only for "this resource does not exist".

    A ``read`` treating any ``ClientError`` as absence cannot distinguish a
    missing resource from a denied one: ``head_bucket`` answers 403 for a bucket
    the caller may not see, and throttling and 5xx arrive the same way. Refresh
    maps a ``None`` read to MISSING and ``--write`` deletes the state row.
    """
    code = error_code(exc)
    return code in _MISSING_CODES or code.endswith(_MISSING_SUFFIX)


@contextlib.contextmanager
def ignore_missing() -> Iterator[None]:
    """Swallow a delete's not-found error so destroy is idempotent.

    A 'creating' state row may point at a resource whose create never reached AWS
    or was already removed; deleting it is then a no-op rather than a hard error.
    """
    try:
        yield
    except ClientError as exc:
        if not is_missing(exc):
            raise


#: Error codes meaning "a resource with this name is already there".
_EXISTS_CODES = frozenset(
    {
        "EntityAlreadyExists",
        "EntityAlreadyExistsException",
        "ResourceConflictException",
        "ResourceInUseException",
        "BucketAlreadyOwnedByYou",
        "HostedZoneAlreadyExists",
        "QueueAlreadyExists",
        "TopicAlreadyExists",
        "ResourceAlreadyExistsException",
        "DistributionAlreadyExists",
    }
)


def create_or_adopt(
    create: Callable[[], dict[str, Any]],
    read: Callable[[], dict[str, Any] | None],
) -> dict[str, Any]:
    """Run ``create``; if the resource already exists, adopt it via ``read``.

    A create is re-run whenever its state row never reached ``created`` — the
    process was killed between the AWS call and the persist, or a sibling node
    failed and cancelled the task. Adoption is keyed on the name ``read`` uses,
    so it resolves to the resource this node declares and no other.
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


def absent_ok(call: Callable[[], _T], *, default: _T | None = None) -> _T | None:
    """Run a read, mapping "this resource does not exist" to ``default``.

    The read-side twin of :func:`ignore_missing`: every handler's ``read`` must
    report absence as ``None`` (refresh classifies it MISSING) while letting a
    denied or throttled call raise — flattening those into absence is how
    ``refresh --prune`` deletes the only record of a healthy resource.
    """
    try:
        return call()
    except ClientError as exc:
        if is_missing(exc):
            return default
        raise


def not_found(res: Resource, op: str, detail: str = "") -> ProviderError:
    """The uniform "resource not found" error for update paths.

    One spelling for a message five handlers wrote five ways — two of them with
    a hardcoded class name one rename away from lying.
    """
    suffix = f" {detail}" if detail else ""
    return ProviderError(
        f"{res.type_name()} not found{suffix}",
        op=op,
        resource_type=res.type_name(),
    )
