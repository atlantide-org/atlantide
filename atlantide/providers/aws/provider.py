"""AWS provider: a dispatcher over per-resource handlers.

boto3 is synchronous; each CRUD call runs in a worker thread via
``asyncio.to_thread`` to fit the async Provider contract without blocking the
scheduler. Clients are cached per ``(alias, service, region)`` — one boto3
``Session`` per alias supplies alternate credentials/endpoint (multi-account),
while region stays a per-resource choice.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, cast

import boto3
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from typing_extensions import override

from atlantide.core import Context, Provider, Resource
from atlantide.core.errors import ProviderError
from atlantide.core.provider import provider_guard
from atlantide.core.tuning import DEFAULT_PARALLELISM
from atlantide.providers.aws.config import boto_config
from atlantide.providers.aws.handlers import HANDLERS, AwsHandler
from atlantide.providers.aws.region import Region

#: Transient AWS failures retried with backoff rather than aborting the apply:
#: throttling, service 5xx, and IAM eventual consistency, where a just-created
#: role is not yet assumable by the service that will use it.
_RETRY_ATTEMPTS = 6
_RETRY_BASE_DELAY = 1.0
_RETRY_MAX_DELAY = 10.0

#: Wall-clock ceiling for one call including all its backoff. Attempt counts alone
#: do not bound elapsed time, and an unbounded retry chain inside a node makes any
#: per-node timeout a lie.
_RETRY_BUDGET = 120.0

#: Error codes that are transient regardless of message (throttling + service 5xx).
_TRANSIENT_CODES = frozenset(
    {
        "Throttling",
        "ThrottlingException",
        "RequestLimitExceeded",
        "TooManyRequestsException",
        "RequestThrottled",
        "SlowDown",
        "InternalError",
        "InternalFailure",
        "ServiceUnavailable",
        "RequestTimeout",
    }
)


#: IAM eventual consistency: the role exists but is not yet assumable or visible.
#: Matched on phrasing, not on the presence of "role": the same error code also
#: carries permanent failures that mention one, and a malformed RoleArn would
#: otherwise burn every attempt before surfacing.
_IAM_PROPAGATION = re.compile(
    r"cannot be assumed"
    r"|not authorized to perform:\s*sts:assumerole"
    r"|does not exist or is not assumable"
    r"|invalid principal",
    re.IGNORECASE,
)

#: IAM propagation settles in seconds — commonly 5-10 of them — so the attempts
#: and the floored backoff below are sized to guarantee that much wall-clock
#: waiting before giving up. Kept separate from the throttling budget: throttling
#: deserves patience, a genuinely wrong role deserves a fast, legible failure.
_IAM_ATTEMPTS = 5

#: Transport failures worth retrying. botocore's adaptive retries (see
#: :mod:`atlantide.providers.aws.config`) handle most of these, but a call that
#: exhausts them still arrives here, and one reset connection should not abort
#: a whole apply.
_TRANSIENT_BOTOCORE = (
    EndpointConnectionError,
    ConnectionClosedError,
    ConnectTimeoutError,
    ReadTimeoutError,
)


def _is_iam_propagation(exc: BaseException) -> bool:
    """The specific race :data:`_IAM_PROPAGATION` names: a role that exists but
    is not yet visible or assumable by the service consuming it."""
    if not isinstance(exc, ClientError):
        return False
    error = exc.response.get("Error", {})
    if error.get("Code", "") != "InvalidParameterValueException":
        return False
    return bool(_IAM_PROPAGATION.search(error.get("Message", "")))


def _is_transient(exc: BaseException) -> bool:
    """Whether ``exc`` is worth another attempt.

    Note the asymmetry: transport errors and throttling are transient regardless
    of what they were doing, while an ``InvalidParameterValueException`` is only
    transient for the one specific race it is used to signal.
    """
    if isinstance(exc, _TRANSIENT_BOTOCORE):
        return True
    if not isinstance(exc, ClientError):
        return False
    code = exc.response.get("Error", {}).get("Code", "")
    if code in _TRANSIENT_CODES:
        return True
    return _is_iam_propagation(exc)


def _attempts_for(exc: BaseException) -> int:
    """How many attempts this class of failure is worth in total."""
    return _IAM_ATTEMPTS if _is_iam_propagation(exc) else _RETRY_ATTEMPTS


#: One handler CRUD call, deferred until :meth:`AwsProvider._call` has resolved
#: which handler owns the resource and which client it needs. Re-invoked per
#: retry attempt, so it must stay side-effect-free up to the boto3 call itself.
_Invoke = Callable[[AwsHandler[Any], Any], Any]


@dataclass(frozen=True, slots=True)
class AwsAlias:
    """A named non-default credential/endpoint profile (one per account)."""

    profile: str | None = None
    endpoint_url: str | None = None


class AwsProvider(Provider):
    name: ClassVar[str] = "aws"
    version: ClassVar[str] = "1.0.0"

    def __init__(
        self,
        *,
        region: str = Region.UsEast1,
        endpoint_url: str | None = None,
        profile: str | None = None,
        aliases: Mapping[str, AwsAlias] | None = None,
        parallelism: int = DEFAULT_PARALLELISM,
    ) -> None:
        self.region = region
        self.endpoint_url = endpoint_url
        self._aliases = dict(aliases or {})
        # Sized here rather than per client: every client this provider makes
        # serves the same apply, so they share one concurrency budget.
        self._config = boto_config(parallelism=parallelism)
        # One Session per alias (``None`` is the default profile/chain), so
        # alternate accounts resolve their own credentials, not the environment's.
        self._sessions: dict[str | None, Any] = {None: boto3.Session(profile_name=profile)}
        self._clients: dict[tuple[str | None, str, str], Any] = {}

    def _session_for(self, alias: str | None) -> Any:
        session = self._sessions.get(alias)
        if session is None:
            if alias not in self._aliases:
                raise ProviderError(
                    f"unknown provider_alias {alias!r} — declare it under [aws.aliases]"
                )
            session = boto3.Session(profile_name=self._aliases[alias].profile)
            self._sessions[alias] = session
        return session

    def _client(self, alias: str | None, service: str, region: str) -> Any:
        key = (alias, service, region)
        client = self._clients.get(key)
        if client is None:
            session = self._session_for(alias)  # validates the alias name first
            endpoint = self._aliases[alias].endpoint_url if alias is not None else self.endpoint_url
            # boto3-stubs overloads client() per literal service name; the service
            # is dynamic here, so go through an untyped factory.
            make_client: Any = session.client
            client = make_client(
                service, region_name=region, endpoint_url=endpoint, config=self._config
            )
            self._clients[key] = client
        return client

    def _dispatch(self, res: Resource, op: str) -> tuple[AwsHandler[Any], Any]:
        handler = HANDLERS.get(res.type_name())
        if handler is None:
            raise ProviderError(f"aws provider cannot {op} {res.type_name()!r}")
        region = handler.region(res) or self.region
        client = self._client(handler.alias(res), handler.service, region)
        return handler, client

    @override
    def identity_field(self, resource_type: type[Resource]) -> str | None:
        """Delegate to the handler that owns this type; unknown types have none."""
        handler = HANDLERS.get(resource_type.type_name())
        return handler.identity_field if handler is not None else None

    async def _call(self, res: Resource, op: str, invoke: _Invoke) -> Any:
        """Dispatch one CRUD op to its handler: guard, thread, retry.

        The four operations differ only in which handler method they call and
        what they pass it; the dispatch/guard/retry sandwich is the same. Each
        caller supplies that difference as ``invoke`` rather than as the method's
        name, so the call it makes is the ordinary typed one
        :class:`AwsHandler` declares — a wrong name or arity is a type error here
        rather than an ``AttributeError`` at apply time. ``op`` remains a string
        because what it labels is a *message*: the failure this is reported as.
        """
        handler, client = self._dispatch(res, op)
        with provider_guard("aws", op, res):
            return await _retrying(invoke, handler, client)

    @override
    async def create(self, ctx: Context, res: Resource) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            await self._call(res, "create", lambda h, client: h.create(client, res)),
        )

    @override
    async def read(self, ctx: Context, res: Resource) -> dict[str, Any] | None:
        return cast(
            "dict[str, Any] | None",
            await self._call(res, "read", lambda h, client: h.read(client, res)),
        )

    @override
    async def update(self, ctx: Context, prior: dict[str, Any], res: Resource) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            await self._call(res, "update", lambda h, client: h.update(client, prior, res)),
        )

    @override
    async def delete(self, ctx: Context, res: Resource) -> None:
        await self._call(res, "delete", lambda h, client: h.delete(client, res))


async def _retrying(fn: Callable[..., Any], *args: Any) -> Any:
    """Run a blocking boto3 call in a thread, retrying transient failures.

    Backoff is *fully jittered* — a uniform draw from ``[0, capped_delay]`` rather
    than the delay itself. Without it, N nodes throttled at the same instant all
    sleep the same amount and retry in lockstep, which is precisely the burst that
    caused the throttling; the herd never disperses. Randomness here is in the
    effect layer and does not touch the determinism guarantees, which are about
    config evaluation.

    A whole-run budget bounds the total, so a call whose every attempt is slow
    cannot quietly outlast the node timeout that is supposed to contain it.
    """
    deadline = time.monotonic() + _RETRY_BUDGET
    attempt = 0
    while True:
        try:
            return await asyncio.to_thread(fn, *args)
        except Exception as exc:
            attempt += 1
            if not _is_transient(exc) or attempt >= _attempts_for(exc):
                raise
            capped = min(_RETRY_BASE_DELAY * 2 ** (attempt - 1), _RETRY_MAX_DELAY)
            # IAM propagation needs wall-clock time, not dispersal: a fully
            # jittered draw can land near zero on every attempt and spend the
            # whole retry budget inside 100ms for a condition that takes seconds.
            # Equal jitter keeps half of each delay as a floor (several seconds
            # guaranteed across _IAM_ATTEMPTS) while still spreading the herd.
            low = capped / 2 if _is_iam_propagation(exc) else 0.0
            delay = random.uniform(low, capped)
            if time.monotonic() + delay >= deadline:
                raise
            await asyncio.sleep(delay)
