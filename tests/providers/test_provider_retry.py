"""AWS provider transient-retry: IAM propagation errors retry, others don't."""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from atlantide.providers.aws.provider import _is_transient, _retrying


def _client_error(code: str, message: str = "") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, "CreateFunction")


def test_is_transient_only_for_role_propagation() -> None:
    assert _is_transient(
        _client_error(
            "InvalidParameterValueException", "The role defined for the function cannot be assumed"
        )
    )
    # same code but unrelated message -> not retried
    assert not _is_transient(_client_error("InvalidParameterValueException", "bad memory size"))
    # a different error code -> not retried
    assert not _is_transient(_client_error("EntityAlreadyExists", "role exists"))


async def test_retrying_recovers_after_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    calls = {"n": 0}

    def fn() -> dict[str, str]:
        calls["n"] += 1
        if calls["n"] < 3:
            raise _client_error("InvalidParameterValueException", "cannot be assumed by Lambda")
        return {"ok": "yes"}

    assert await _retrying(fn) == {"ok": "yes"}
    assert calls["n"] == 3  # failed twice, succeeded on the third


async def test_retrying_reraises_non_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("asyncio.sleep", _no_sleep)

    def fn() -> dict[str, str]:
        raise _client_error("EntityAlreadyExists", "already exists")

    with pytest.raises(ClientError):
        await _retrying(fn)


async def _no_sleep(_delay: float) -> None:
    return None
