"""DynamoDB and boto3 plumbing shared by the S3 collaborators."""

from __future__ import annotations

import random
import time
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Self

import boto3
from botocore.exceptions import ClientError

from atlantide.state.s3 import limits
from atlantide.util.aws import client_config, error_code

__all__ = [
    "CAS_CODES",
    "CLIENT_CONFIG",
    "MISSING_CODES",
    "Budget",
    "Clients",
    "backoff",
    "cancellation_codes",
    "contended",
    "ddb_num",
    "ddb_str",
    "failed_items",
    "head_update",
    "parallel",
    "transact",
]


#: Bounded, retried clients: without a read timeout, a hung state call blocks the
#: run while it holds its lease. The pool is sized for concurrent writes.
CLIENT_CONFIG = client_config(max_pool=64)

#: Error codes S3 returns when a conditional write loses the race. The preflight
#: probe is their behavioural test.
CAS_CODES = frozenset({"PreconditionFailed", "ConditionalRequestConflict"})

#: Error codes S3 returns for an object that does not exist (``GetObject`` says
#: ``NoSuchKey``; ``HeadObject`` has no body, so only ``404``/``NotFound``).
MISSING_CODES = frozenset({"NoSuchKey", "404", "NotFound"})


@dataclass(slots=True)
class Clients:
    """The two boto3 clients, held (not copied) by every collaborator.

    Read through this holder on every call and never cached: tests swap the
    clients for wrapping ones after the backend is built.
    """

    s3: Any
    ddb: Any

    @classmethod
    def connect(cls, *, profile: str | None, region: str | None, endpoint_url: str | None) -> Self:
        # `boto3.Session` is looked up on each call so a test can patch it.
        session = boto3.Session(profile_name=profile, region_name=region)
        # boto3-stubs overloads client() per literal service name; these are built
        # dynamically, so go through an untyped factory.
        make_client: Any = session.client
        return cls(
            s3=make_client("s3", endpoint_url=endpoint_url, config=CLIENT_CONFIG),
            ddb=make_client("dynamodb", endpoint_url=endpoint_url, config=CLIENT_CONFIG),
        )


def ddb_num(value: int | float) -> dict[str, str]:
    """A DynamoDB number attribute (a float keeps its ``repr`` precision)."""
    return {"N": repr(value) if isinstance(value, float) else str(value)}


def ddb_str(value: str) -> dict[str, str]:
    return {"S": value}


def contended(exc: ClientError) -> bool:
    """Whether a cancelled transaction was refused by a *condition*.

    DynamoDB also cancels transactions for transient reasons (item-level
    conflicts, throttling), which callers retry instead of treating them as
    another run holding the lease.
    """
    reasons = exc.response.get("CancellationReasons") or []
    if not reasons:
        return True  # older/unknown response shape: assume contention
    return any(reason.get("Code") == "ConditionalCheckFailed" for reason in reasons)


def cancellation_codes(exc: ClientError) -> list[str]:
    """The distinct reason codes of a cancelled transaction, sorted.

    DynamoDB reports ``"None"`` for items that did not cause the cancellation;
    those are left out.
    """
    reasons = exc.response.get("CancellationReasons") or []
    codes = {str(reason.get("Code", "None")) for reason in reasons}
    return sorted(codes - {"None"})


def failed_items(exc: ClientError) -> list[int]:
    """Indexes of the transaction items whose condition failed."""
    reasons = exc.response.get("CancellationReasons") or []
    return [i for i, reason in enumerate(reasons) if reason.get("Code") == "ConditionalCheckFailed"]


def backoff(attempt: int) -> None:
    """Sleep before retry ``attempt + 1``: full jitter under an exponential cap.

    Tests make it instant by patching :data:`limits.BACKOFF_BASE` to ``0``.
    """
    delay = min(limits.BACKOFF_CAP, limits.BACKOFF_BASE * 2**attempt)
    time.sleep(random.uniform(0, delay))


@dataclass(slots=True)
class Budget:
    """The ``TransactWriteItems`` calls one transaction may make.

    Shared by :func:`transact`'s transient retries and its caller's retries of
    contended cancellations, so the two never multiply.
    """

    left: int = field(default_factory=lambda: limits.DDB_ATTEMPTS)
    #: Transient cancellations retried so far: the backoff's exponent.
    retries: int = 0


def transact(
    ddb: Any, items: list[dict[str, Any]], budget: Budget | None = None
) -> ClientError | None:
    """``TransactWriteItems``, retrying transient cancellations with backoff.

    ``None`` once it applies; the cancellation when a *condition* refused it
    (:func:`contended`). Any other error, and the last transient cancellation
    once ``budget`` (by default a fresh one) is spent, is raised.
    """
    budget = budget if budget is not None else Budget()
    while True:
        budget.left -= 1
        try:
            ddb.transact_write_items(TransactItems=items, ClientRequestToken=uuid.uuid4().hex)
            return None
        except ClientError as exc:
            if error_code(exc) != "TransactionCanceledException":
                raise
            if contended(exc):
                return exc
            # Cancelled for a transient reason (item conflict, throttling), not
            # by a condition: retry.
            if budget.left <= 0:
                raise
            backoff(budget.retries)
            budget.retries += 1


_HEAD_SET = "SET #s = :seq, #r = :ref, #op = :op, state_ns = :ns"
_HEAD_NAMES = {"#s": "seq", "#r": "ref", "#op": "op"}


def head_update(  # noqa: PLR0913 - keyword-only fields of one UpdateItem
    table: str,
    key: str,
    *,
    seq: int,
    ref: str,
    op: str,
    namespace: str,
    expect: int | None = None,
    fence: int | None = None,
    seed_fence: int | None = None,
) -> dict[str, Any]:
    """The ``UpdateItem`` arguments pointing a head at a journal entry.

    With ``expect`` the head must be at that seq or never written (a commit on
    top of what the writer read); without it, the head must have no seq at all
    (a rebuild of a lost head). ``fence`` also requires the head at exactly that
    fence (a leased commit); ``seed_fence`` sets the fence only where the head
    has none (a rebuild).
    """
    if fence is not None and seed_fence is not None:  # pragma: no cover - misuse
        raise ValueError("a head update checks a fence or seeds one, not both")
    update = _HEAD_SET
    values: dict[str, Any] = {}
    if expect is None:
        condition = "attribute_not_exists(#s)"
    else:
        condition = "(#s = :expect OR attribute_not_exists(#s))"
        values[":expect"] = ddb_num(expect)
    values |= {
        ":seq": ddb_num(seq),
        ":ref": ddb_str(ref),
        ":op": ddb_str(op),
        ":ns": ddb_str(namespace),
    }
    if fence is not None:
        condition += " AND fence = :f"
        values[":f"] = ddb_num(fence)
    if seed_fence is not None:
        update += ", fence = if_not_exists(fence, :f)"
        values[":f"] = ddb_num(seed_fence)
    return {
        "TableName": table,
        "Key": {"node_id": ddb_str(key)},
        "UpdateExpression": update,
        "ConditionExpression": condition,
        "ExpressionAttributeNames": dict(_HEAD_NAMES),
        "ExpressionAttributeValues": values,
    }


def parallel[T, R](work: Callable[[T], R], items: Sequence[T]) -> list[R]:
    """``[work(item) for item in items]``, fanned out when there is enough of it."""
    if len(items) <= 2:
        return [work(item) for item in items]
    pool = ThreadPoolExecutor(max_workers=min(limits.READ_FANOUT, len(items)))
    try:
        results = list(pool.map(work, items))
    except BaseException:
        # Fail fast: drop the tasks not started instead of running them all.
        pool.shutdown(cancel_futures=True)
        raise
    pool.shutdown()
    return results
