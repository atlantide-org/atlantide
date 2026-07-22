"""Cloud-provider test kit: reusable env + mock + default Stack setup.

Adding a new cloud provider's suite is three lines — supply the provider's env
vars and a ``mock_factory`` (e.g. ``moto.mock_aws``); everything else is shared.

:func:`fake_aws_credentials` and :func:`create_state_store` are the pieces the
remote-state suites compose with ``moto.mock_aws`` directly, since they need no
``Stack``.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from typing import Any

import boto3
import pytest

from atlantide.core import Stack

#: Region every AWS-backed test suite runs in unless it says otherwise.
TEST_REGION = "eu-north-1"


def cloud_env_fixture(
    env: dict[str, str],
    *,
    region: str,
    mock_factory: Callable[[], AbstractContextManager[object]],
    stack: str = "default",
) -> Callable[..., Iterator[None]]:
    """An autouse pytest fixture bound to a suite's env + mock + default Stack.

    ``mock_factory`` is the seam: ``moto.mock_aws`` for AWS, any context-manager
    factory for the next provider.
    """

    @pytest.fixture(autouse=True)
    def _fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        with mock_factory(), Stack(stack, region=region):
            yield

    return _fixture


def fake_aws_credentials(
    monkeypatch: pytest.MonkeyPatch, *, region: str = TEST_REGION
) -> None:
    """Stop botocore reaching for real credentials or a real region under moto."""
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(key, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", region)


def create_state_store(
    bucket: str, lock_table: str, *, region: str = TEST_REGION
) -> None:
    """Create the bucket and lock table the s3 state backend expects to exist.

    The backend deliberately does not create them (they are the trust root for
    shared state), so every suite that exercises it must stand them up first.
    """
    s3: Any = boto3.client("s3", region_name=region)
    s3.create_bucket(
        Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": region}
    )
    ddb: Any = boto3.client("dynamodb", region_name=region)
    ddb.create_table(
        TableName=lock_table,
        KeySchema=[{"AttributeName": "node_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "node_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
