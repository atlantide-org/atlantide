"""AWS provider transient-retry: IAM propagation errors retry, others don't."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from atlantide.providers.aws.provider import _is_transient, _retrying
from tests.providers.aws.conftest import client_error


def test_is_transient_only_for_role_propagation() -> None:
    assert _is_transient(
        client_error(
            "InvalidParameterValueException", "The role defined for the function cannot be assumed"
        )
    )
    # same code but unrelated message -> not retried
    assert not _is_transient(client_error("InvalidParameterValueException", "bad memory size"))
    # a different error code -> not retried
    assert not _is_transient(client_error("EntityAlreadyExists", "role exists"))


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"Error": {}},
        {"Error": {"Code": None, "Message": None}},
        {"Error": {"Code": "InvalidParameterValueException"}},
        {"Error": {"Code": "InvalidParameterValueException", "Message": None}},
    ],
)
def test_an_odd_error_shape_is_not_transient(response: Any) -> None:
    """A response missing its code or message is a permanent failure, not a crash."""
    assert not _is_transient(ClientError(response, "CreateFunction"))


async def test_retrying_recovers_after_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    calls = {"n": 0}

    def fn() -> dict[str, str]:
        calls["n"] += 1
        if calls["n"] < 3:
            raise client_error("InvalidParameterValueException", "cannot be assumed by Lambda")
        return {"ok": "yes"}

    assert await _retrying(fn) == {"ok": "yes"}
    assert calls["n"] == 3  # failed twice, succeeded on the third


async def test_retrying_reraises_non_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("asyncio.sleep", _no_sleep)

    def fn() -> dict[str, str]:
        raise client_error("EntityAlreadyExists", "already exists")

    with pytest.raises(ClientError):
        await _retrying(fn)


def _record_draws(
    windows: list[tuple[float, float]], pick: Callable[[float, float], float]
) -> Callable[[float, float], float]:
    """A `random.uniform` stand-in that records each [low, high] window it is asked for."""

    def uniform(low: float, high: float) -> float:
        windows.append((low, high))
        return pick(low, high)

    return uniform


async def _no_sleep(_delay: float) -> None:
    return None


# -- backoff ------------------------------------------------------------------


async def test_backoff_is_fully_jittered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without jitter, N nodes throttled at the same instant retry in lockstep,
    reproducing the burst that caused the throttling.

    Full jitter means the sleep is drawn from [0, capped), not set to capped.
    """
    slept: list[float] = []

    async def record(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr("asyncio.sleep", record)
    monkeypatch.setattr(
        "atlantide.providers.aws.provider.random.uniform",
        lambda low, high: high,  # take the ceiling, so the cap is observable
    )

    def always_throttled() -> None:
        raise client_error("Throttling", "slow down")

    with pytest.raises(ClientError):
        await _retrying(always_throttled)

    assert slept == [1.0, 2.0, 4.0, 8.0, 10.0], "exponential, capped at the max delay"


async def test_the_jitter_window_is_the_capped_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every sleep is a draw from [0, capped], never a fixed value."""
    windows: list[tuple[float, float]] = []
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    monkeypatch.setattr(
        "atlantide.providers.aws.provider.random.uniform",
        _record_draws(windows, lambda low, high: 0.0),
    )

    def always_throttled() -> None:
        raise client_error("Throttling", "slow down")

    with pytest.raises(ClientError):
        await _retrying(always_throttled)

    assert all(low == 0.0 for low, _ in windows)
    assert [high for _, high in windows] == [1.0, 2.0, 4.0, 8.0, 10.0]


# -- what counts as transient -------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        EndpointConnectionError(endpoint_url="https://s3.amazonaws.com"),
        ConnectionClosedError(endpoint_url="https://s3.amazonaws.com"),
        ConnectTimeoutError(endpoint_url="https://s3.amazonaws.com"),
        ReadTimeoutError(endpoint_url="https://s3.amazonaws.com"),
    ],
)
async def test_transport_failures_are_retried(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    """These are not `ClientError`, but they are transient: without a retry, one
    dropped connection aborts a long apply on an otherwise healthy network."""
    assert _is_transient(exc)

    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise exc
        return "ok"

    assert await _retrying(flaky) == "ok"
    assert calls["n"] == 2


def test_a_malformed_role_arn_is_not_mistaken_for_propagation() -> None:
    """Guards against retrying every `InvalidParameterValueException` mentioning
    "role", which makes a permanently bad ARN spend the full backoff first."""
    assert not _is_transient(
        client_error("InvalidParameterValueException", "Invalid RoleArn format")
    )
    assert not _is_transient(
        client_error("InvalidParameterValueException", "The role name is too long")
    )


@pytest.mark.parametrize(
    "message",
    [
        "The role defined for the function cannot be assumed by Lambda",
        "User: arn:aws:iam::1:user/x is not authorized to perform: sts:AssumeRole",
        "Role does not exist or is not assumable",
        "Invalid principal in policy",
    ],
)
def test_real_iam_propagation_phrasings_are_retried(message: str) -> None:
    assert _is_transient(client_error("InvalidParameterValueException", message))


async def test_iam_propagation_gives_up_sooner_than_throttling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Propagation settles in seconds. Spending the throttling budget on it turns
    a legible misconfiguration into a half-minute hang."""
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    calls = {"n": 0}

    def never_settles() -> None:
        calls["n"] += 1
        raise client_error("InvalidParameterValueException", "cannot be assumed")

    with pytest.raises(ClientError):
        await _retrying(never_settles)
    assert calls["n"] == 5  # not the 6 that throttling gets


async def test_iam_propagation_delays_are_floored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fully jittered draw can land near zero on every attempt, spending all
    the IAM attempts inside 100ms for a condition that commonly needs 5-10s.
    Propagation keeps three quarters of each capped delay as a floor, so even
    the worst draws wait out the whole window."""
    windows: list[tuple[float, float]] = []
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    monkeypatch.setattr(
        "atlantide.providers.aws.provider.random.uniform",
        _record_draws(windows, lambda low, high: low),  # the worst draw
    )

    def never_settles() -> None:
        raise client_error("InvalidParameterValueException", "cannot be assumed")

    with pytest.raises(ClientError):
        await _retrying(never_settles)

    assert windows == [(0.75, 1.0), (1.5, 2.0), (3.0, 4.0), (6.0, 8.0)]
    assert sum(low for low, _ in windows) >= 10.0, "worst case still spans propagation"


async def test_throttling_jitter_is_not_floored(monkeypatch: pytest.MonkeyPatch) -> None:
    """The floor is for propagation only: throttled callers must be free to draw
    near-zero delays or the herd never disperses."""
    windows: list[tuple[float, float]] = []
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    monkeypatch.setattr(
        "atlantide.providers.aws.provider.random.uniform",
        _record_draws(windows, lambda low, high: 0.0),
    )

    def always_throttled() -> None:
        raise client_error("Throttling", "slow down")

    with pytest.raises(ClientError):
        await _retrying(always_throttled)

    assert all(low == 0.0 for low, _ in windows)


async def test_the_wall_clock_budget_stops_a_slow_retry_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attempt counts do not bound elapsed time. Without a budget, a retry chain
    can outlast the node timeout meant to contain it."""
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    monkeypatch.setattr("atlantide.providers.aws.provider._RETRY_BUDGET", 0.0)

    calls = {"n": 0}

    def always_throttled() -> None:
        calls["n"] += 1
        raise client_error("Throttling", "slow down")

    with pytest.raises(ClientError):
        await _retrying(always_throttled)
    assert calls["n"] == 1, "the budget was spent before the first backoff"
