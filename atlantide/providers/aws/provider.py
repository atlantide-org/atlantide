"""AWS provider: a dispatcher over per-resource handlers.

boto3 is synchronous, so each CRUD call runs in a worker thread via
``asyncio.to_thread`` to fit the async Provider contract without blocking the
scheduler. Clients are cached per ``(alias, service, region)``: each alias has its
own boto3 ``Session`` with alternate credentials or endpoint (multi-account), and
region is chosen per resource.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, cast, override

from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from atlantide.core import Context, Provider, Resource
from atlantide.core.errors import ProviderError
from atlantide.core.provider import provider_guard
from atlantide.core.tuning import DEFAULT_PARALLELISM
from atlantide.providers.aws.config import boto_config
from atlantide.providers.aws.handlers import HANDLERS, AwsHandler
from atlantide.providers.aws.region import Region
from atlantide.util.aws import error_code, error_message

#: Attempts and backoff bounds (seconds) for transient AWS failures: throttling,
#: service 5xx and transport errors.
_RETRY_ATTEMPTS = 6
_RETRY_BASE_DELAY = 1.0
_RETRY_MAX_DELAY = 10.0

#: Wall-clock budget in seconds for starting another attempt of one call. It is
#: checked before each backoff sleep: no retry is scheduled whose sleep would end
#: past the budget. An attempt already running is not interrupted, so the total can
#: exceed the budget by the last attempt's duration (bounded by the botocore read
#: timeout and transport retries in :mod:`atlantide.providers.aws.config`).
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
#: Matched on specific phrasing, since the same error code also carries permanent
#: role failures (e.g. a malformed RoleArn) that must not be retried.
_IAM_PROPAGATION = re.compile(
    r"cannot be assumed"
    r"|not authorized to perform:\s*sts:assumerole"
    r"|does not exist or is not assumable"
    r"|invalid principal",
    re.IGNORECASE,
)

#: IAM propagation typically settles in 5-10 seconds. With the delays floored at
#: :data:`_IAM_DELAY_FLOOR` of each cap, these attempts wait at least 11.25 s in
#: total (0.75 + 1.5 + 3 + 6), covering that window. Separate from
#: :data:`_RETRY_ATTEMPTS` so a wrong role fails fast.
_IAM_ATTEMPTS = 5

#: Fraction of each capped delay kept as a floor for IAM propagation retries.
_IAM_DELAY_FLOOR = 0.75

#: Transport failures to retry. botocore's adaptive retries
#: (:mod:`atlantide.providers.aws.config`) handle most of these; a call that
#: exhausts them is retried here as well.
_TRANSIENT_BOTOCORE = (
    EndpointConnectionError,
    ConnectionClosedError,
    ConnectTimeoutError,
    ReadTimeoutError,
)


def _is_iam_propagation(exc: BaseException) -> bool:
    """Whether ``exc`` is the IAM propagation race that :data:`_IAM_PROPAGATION` matches."""
    if not isinstance(exc, ClientError) or error_code(exc) != "InvalidParameterValueException":
        return False
    return bool(_IAM_PROPAGATION.search(error_message(exc)))


def _is_transient(exc: BaseException) -> bool:
    """Whether ``exc`` is worth another attempt.

    Transport errors and throttling are always transient; an
    ``InvalidParameterValueException`` is transient only for the IAM propagation
    race.
    """
    if isinstance(exc, _TRANSIENT_BOTOCORE):
        return True
    if not isinstance(exc, ClientError):
        return False
    return error_code(exc) in _TRANSIENT_CODES or _is_iam_propagation(exc)


def _attempts_for(exc: BaseException) -> int:
    """Total attempts allowed for this class of failure."""
    return _IAM_ATTEMPTS if _is_iam_propagation(exc) else _RETRY_ATTEMPTS


#: One handler CRUD call, invoked by :meth:`AwsProvider._call` with the resolved
#: handler and client. It runs once per retry attempt, so it must have no side
#: effects before the boto3 call.
type _Invoke = Callable[[AwsHandler[Any], Any], Any]


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
        # One config for every client: they serve the same apply and share one
        # concurrency budget.
        self._config = boto_config(parallelism=parallelism)
        self._profile = profile
        # One Session per alias (``None`` is the default profile/chain), so each
        # account resolves its own credentials. Sessions are built lazily: every
        # command builds a provider registry, and ``plan`` makes no AWS calls.
        self._sessions: dict[str | None, Any] = {}
        self._clients: dict[tuple[str | None, str, str], Any] = {}

    def _session_for(self, alias: str | None) -> Any:
        session = self._sessions.get(alias)
        if session is None:
            if alias is None:
                profile = self._profile
            elif alias in self._aliases:
                profile = self._aliases[alias].profile
            else:
                raise ProviderError(
                    f"unknown provider_alias {alias!r} — declare it under [aws.aliases]"
                )
            # Lazy import: boto3 takes ~45ms to import, and provider discovery
            # loads this module for every command.
            import boto3

            session = boto3.Session(profile_name=profile)
            self._sessions[alias] = session
        return session

    def _client(self, alias: str | None, service: str, region: str) -> Any:
        key = (alias, service, region)
        client = self._clients.get(key)
        if client is None:
            session = self._session_for(alias)  # validates the alias name first
            endpoint = self._aliases[alias].endpoint_url if alias is not None else self.endpoint_url
            # boto3-stubs overloads client() per literal service name; the service
            # is dynamic here, so the call goes through an untyped reference.
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
        """Dispatch one CRUD op to its handler under the error guard, in a thread, with retries.

        ``invoke`` is a typed call on :class:`AwsHandler`, so a wrong method name or
        arity is a type error rather than a runtime ``AttributeError``. ``op`` only
        labels error messages.
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

    Backoff is fully jittered (a uniform draw from ``[0, capped_delay]``) so nodes
    throttled together do not retry in lockstep. The randomness is confined to the
    effect layer; the determinism guarantees cover config evaluation only.
    :data:`_RETRY_BUDGET` stops scheduling retries once a backoff would end past
    it; it does not cut short an attempt in progress.
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
            # IAM propagation needs elapsed time: full jitter can draw near-zero
            # delays on every attempt, so its draws are floored.
            low = capped * _IAM_DELAY_FLOOR if _is_iam_propagation(exc) else 0.0
            delay = random.uniform(low, capped)
            if time.monotonic() + delay >= deadline:
                raise
            await asyncio.sleep(delay)
