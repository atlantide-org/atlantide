"""The mocked AWS every S3 backend test runs against."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from moto import mock_aws

from tests.support import create_state_store, fake_aws_credentials

from ..conftest import BUCKET, LOCK_TABLE, REGION


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Credentials, a mocked AWS, and the bucket + lock table already created."""
    fake_aws_credentials(monkeypatch, region=REGION)
    with mock_aws():
        create_state_store(BUCKET, LOCK_TABLE, region=REGION)
        yield
