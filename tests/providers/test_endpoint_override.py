"""``aws_endpoint``: sending every AWS call somewhere other than AWS.

The option is what makes an emulator or a private endpoint usable at all, and it
is silent when wrong — a client built without the override just talks to the real
account, which is the failure you least want to discover by observing the bill.

**Scope, stated plainly.** These tests prove the endpoint reaches every client
atlantide builds, for every service, region and alias. They do *not* prove
atlantide works against LocalStack, MinIO, or any other AWS-compatible
implementation: none is in the test suite, and the provider's fidelity to those
is unverified. The README says so too. That distinction is the whole point of
this file — the wiring is a promise worth keeping; compatibility is not one
being made.
"""

from __future__ import annotations

from typing import Any

import pytest

from atlantide.providers.aws import AwsProvider
from atlantide.providers.aws.provider import AwsAlias

ENDPOINT = "http://localhost:4566"


class _RecordingSession:
    """A boto3 Session stand-in that records how each client was asked for."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def client(self, service: str, **kwargs: Any) -> object:
        self.calls.append({"service": service, **kwargs})
        return object()


def _provider_with(session: _RecordingSession, **kwargs: Any) -> AwsProvider:
    provider = AwsProvider(**kwargs)
    provider._sessions = {None: session}  # type: ignore[assignment]
    return provider


def test_the_endpoint_reaches_the_client() -> None:
    session = _RecordingSession()
    provider = _provider_with(session, endpoint_url=ENDPOINT)

    provider._client(None, "s3", "eu-north-1")

    assert session.calls[0]["endpoint_url"] == ENDPOINT


def test_no_endpoint_means_no_override() -> None:
    """`None` is what tells botocore to resolve the real AWS endpoint. Passing
    an empty string instead would be a different, broken thing."""
    session = _RecordingSession()
    provider = _provider_with(session)

    provider._client(None, "s3", "eu-north-1")

    assert session.calls[0]["endpoint_url"] is None


@pytest.mark.parametrize("service", ["s3", "iam", "lambda", "sqs", "dynamodb"])
def test_every_service_is_redirected(service: str) -> None:
    """A per-service exemption would send some calls to the emulator and the rest
    to the real account — the worst possible half-state."""
    session = _RecordingSession()
    provider = _provider_with(session, endpoint_url=ENDPOINT)

    provider._client(None, service, "eu-north-1")

    assert session.calls[0]["endpoint_url"] == ENDPOINT


def test_a_cached_client_keeps_the_override() -> None:
    """Clients are cached per (alias, service, region). A cache that returned an
    un-overridden client for a second region would leak calls to real AWS."""
    session = _RecordingSession()
    provider = _provider_with(session, endpoint_url=ENDPOINT)

    provider._client(None, "s3", "eu-north-1")
    provider._client(None, "s3", "us-east-1")

    assert [call["endpoint_url"] for call in session.calls] == [ENDPOINT, ENDPOINT]
    assert [call["region_name"] for call in session.calls] == ["eu-north-1", "us-east-1"]


def test_an_alias_uses_its_own_endpoint_not_the_default() -> None:
    """An alias is a separate account. Inheriting the default endpoint would
    point another account's resources at the wrong place entirely."""
    session = _RecordingSession()
    provider = _provider_with(
        session,
        endpoint_url=ENDPOINT,
        aliases={"other": AwsAlias(endpoint_url="http://localhost:9999")},
    )
    provider._sessions["other"] = session

    provider._client("other", "s3", "eu-north-1")

    assert session.calls[0]["endpoint_url"] == "http://localhost:9999"


def test_an_alias_without_an_endpoint_does_not_borrow_the_default() -> None:
    """Silence means "the real AWS endpoint for that account", which is the only
    reading that cannot surprise someone."""
    session = _RecordingSession()
    provider = _provider_with(session, endpoint_url=ENDPOINT, aliases={"other": AwsAlias()})
    provider._sessions["other"] = session

    provider._client("other", "s3", "eu-north-1")

    assert session.calls[0]["endpoint_url"] is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
