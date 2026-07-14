"""AWS provider: a dispatcher over per-resource handlers.

boto3 is synchronous; each CRUD call runs in a worker thread via
``asyncio.to_thread`` to fit the async Provider contract without blocking the
scheduler. Clients are cached per ``(alias, service, region)`` — one boto3
``Session`` per alias supplies alternate credentials/endpoint (multi-account),
while region stays a per-resource choice.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar, cast

import boto3
from botocore.exceptions import ClientError

from atlantide.core import Context, Provider, Resource
from atlantide.core.errors import ProviderError
from atlantide.core.provider import provider_guard
from atlantide.providers.aws.handlers import HANDLERS, AwsHandler
from atlantide.providers.aws.region import Region

#: Retry transient AWS failures with backoff instead of aborting the apply:
#: throttling / 5xx, plus IAM eventual consistency (a just-created role isn't
#: immediately assumable by the service that will use it, e.g. Lambda).
_RETRY_ATTEMPTS = 6
_RETRY_BASE_DELAY = 1.0

#: Error codes that are transient regardless of message (throttling + service 5xx).
_TRANSIENT_CODES = frozenset({
    "Throttling", "ThrottlingException", "RequestLimitExceeded",
    "TooManyRequestsException", "RequestThrottled", "SlowDown",
    "InternalError", "InternalFailure", "ServiceUnavailable", "RequestTimeout",
})


def _is_transient(exc: ClientError) -> bool:
    error = exc.response.get("Error", {})
    code = error.get("Code", "")
    if code in _TRANSIENT_CODES:
        return True
    # IAM eventual consistency: the role exists but isn't assumable/visible yet.
    if code == "InvalidParameterValueException":
        message = error.get("Message", "").lower()
        return "assume" in message or "role" in message
    return False


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
    ) -> None:
        self.region = region
        self.endpoint_url = endpoint_url
        self._aliases = dict(aliases or {})
        # One Session per alias (``None`` = the default profile/chain), so alternate
        # accounts get their own credentials without env-only assumptions.
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
            client = make_client(service, region_name=region, endpoint_url=endpoint)
            self._clients[key] = client
        return client

    def _dispatch(self, res: Resource, op: str) -> tuple[AwsHandler[Any], Any]:
        handler = HANDLERS.get(res.type_name())
        if handler is None:
            raise ProviderError(f"aws provider cannot {op} {res.type_name()!r}")
        region = handler.region(res) or self.region
        client = self._client(handler.alias(res), handler.service, region)
        return handler, client

    async def create(self, ctx: Context, res: Resource) -> dict[str, Any]:
        handler, client = self._dispatch(res, "create")
        with provider_guard("aws", "create", res):
            return cast("dict[str, Any]", await _retrying(handler.create, client, res))

    async def read(self, ctx: Context, res: Resource) -> dict[str, Any] | None:
        handler, client = self._dispatch(res, "read")
        with provider_guard("aws", "read", res):
            return cast("dict[str, Any] | None", await _retrying(handler.read, client, res))

    async def update(self, ctx: Context, prior: dict[str, Any], res: Resource) -> dict[str, Any]:
        handler, client = self._dispatch(res, "update")
        with provider_guard("aws", "update", res):
            return cast(
                "dict[str, Any]", await _retrying(handler.update, client, prior, res)
            )

    async def delete(self, ctx: Context, res: Resource) -> None:
        handler, client = self._dispatch(res, "delete")
        with provider_guard("aws", "delete", res):
            await _retrying(handler.delete, client, res)


async def _retrying(fn: Callable[..., Any], *args: Any) -> Any:
    """Run a blocking boto3 call in a thread, retrying transient IAM-propagation
    errors with exponential backoff (up to :data:`_RETRY_ATTEMPTS`)."""
    delay = _RETRY_BASE_DELAY
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return await asyncio.to_thread(fn, *args)
        except ClientError as exc:
            if not _is_transient(exc) or attempt == _RETRY_ATTEMPTS - 1:
                raise
            await asyncio.sleep(delay)
            delay = min(delay * 2, 10.0)
