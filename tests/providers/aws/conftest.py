"""Fixtures and helpers shared by the AWS provider suites.

The service suites (``test_s3.py``, ``test_iam.py``, ...) opt into :func:`aws_env`
with ``pytestmark``. It is deliberately not autouse: the suites here that stub
boto3 instead of mocking AWS (retry, endpoint override, client config, the pure
helpers) must not silently run under moto too. A suite that needs another region
defines its own ``aws_env = aws_fixture(...)``, which overrides this one.

The plain helpers are imported explicitly (``from tests.providers.aws.conftest
import ...``), the way the root ``make_engine`` is.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from atlantide.core import Stack
from atlantide.engine import Engine
from atlantide.providers import aws, local
from atlantide.providers.aws import AwsProvider
from atlantide.providers.local import LocalProvider
from tests.conftest import make_engine
from tests.support import TEST_REGION

#: The service suites' region. us-east-1 is where S3 creates a bucket without a
#: ``LocationConstraint``, which the bucket outputs and several engine graphs
#: assert on.
REGION = "us-east-1"

#: An assume-role trust policy for EC2; :data:`LAMBDA_TRUST` is the Lambda one.
TRUST_POLICY = (
    '{"Version": "2012-10-17", "Statement": [{"Effect": "Allow",'
    ' "Principal": {"Service": "ec2.amazonaws.com"}, "Action": "sts:AssumeRole"}]}'
)
LAMBDA_TRUST = TRUST_POLICY.replace("ec2.amazonaws.com", "lambda.amazonaws.com")


@pytest.fixture
def aws_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """moto, fake credentials and a ``default`` stack in :data:`REGION`.

    The same setup as ``tests.support.aws_fixture(region=REGION)``: resources
    require a region, and the stack supplies it while keeping the node-id prefix
    ``default``.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws(), Stack("default", region=REGION):
        yield


@pytest.fixture
def package(tmp_path: Path) -> str:
    """A real deployment package on disk. Lambda has no placeholder: a function
    with no code is refused rather than silently shipped empty."""
    source = tmp_path / "src"
    source.mkdir()
    (source / "index.py").write_text("def handler(event, context):\n    return {}\n")
    return str(source)


def mixed_engine() -> Engine:
    """An engine over the local and AWS providers together."""
    return make_engine({**local.TYPES, **aws.TYPES}, LocalProvider(), AwsProvider())


def exists(bucket: str) -> bool:
    """Whether ``bucket`` is among the account's buckets."""
    names = {b["Name"] for b in boto3.client("s3").list_buckets()["Buckets"]}
    return bucket in names


def ec2_client() -> Any:
    """An EC2 client in :data:`~tests.support.TEST_REGION`."""
    return boto3.client("ec2", region_name=TEST_REGION)


def client_error(code: str, message: str = "") -> ClientError:
    """A botocore ``ClientError`` carrying ``code`` and ``message``."""
    return ClientError({"Error": {"Code": code, "Message": message}}, "CreateFunction")
