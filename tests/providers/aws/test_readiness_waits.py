"""Handlers wait for a resource to settle before its follow-up calls.

On real AWS a just-created DynamoDB table is ``CREATING`` (so
``update_time_to_live`` raises ``ResourceInUseException``), and a Lambda whose
configuration update is ``InProgress`` rejects the code upload with
``ResourceConflictException``. moto's resources are ready instantly, so these
tests assert the ordering (the waiter runs between the create/update and its
follow-ups) through a spy that records every client call.
"""

from __future__ import annotations

from typing import Any

import boto3

from atlantide.providers.aws import DynamoDbTable, LambdaFunction
from atlantide.providers.aws.handlers.compute import LambdaFunctionHandler
from atlantide.providers.aws.handlers.database import DynamoDbTableHandler
from tests.providers.aws.conftest import LAMBDA_TRUST
from tests.support import TEST_REGION, aws_fixture

aws_env = aws_fixture()


class _Spy:
    """Records the order of boto3 calls and passes everything through.

    ``get_waiter`` is recorded as ``waiter:<name>``; the waiter itself is the
    real one, polling through the unwrapped client.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            if name == "get_waiter":
                self.calls.append(f"waiter:{args[0]}")
            else:
                self.calls.append(name)
            return attr(*args, **kwargs)

        return call

    def index(self, call: str) -> int:
        assert call in self.calls, f"{call!r} never ran (calls: {self.calls})"
        return self.calls.index(call)


def test_dynamodb_create_waits_for_active_before_ttl_and_pitr() -> None:
    client = _Spy(boto3.client("dynamodb", region_name=TEST_REGION))
    res = DynamoDbTable(
        "d",
        table_name="items",
        hash_key="pk",
        ttl_attribute="expires",
        point_in_time_recovery=True,
    )
    DynamoDbTableHandler().create(client, res)

    settled = client.index("waiter:table_exists")
    assert client.index("create_table") < settled
    assert settled < client.index("describe_time_to_live")
    assert settled < client.index("update_continuous_backups")


def _role_arn() -> str:
    iam = boto3.client("iam", region_name=TEST_REGION)
    return str(
        iam.create_role(RoleName="fn-role", AssumeRolePolicyDocument=LAMBDA_TRUST)["Role"]["Arn"]
    )


def test_lambda_update_waits_for_config_to_settle_before_code(package: str) -> None:
    handler = LambdaFunctionHandler()
    real = boto3.client("lambda", region_name=TEST_REGION)
    res = LambdaFunction("f", function_name="fn", role_arn=_role_arn(), code_path=package)
    prior = handler.create(real, res)

    client = _Spy(real)
    handler.update(client, prior, res)

    settled = client.index("waiter:function_updated_v2")
    assert client.index("update_function_configuration") < settled
    assert settled < client.index("update_function_code")


def test_lambda_update_without_code_does_not_wait(package: str) -> None:
    """The wait exists only to protect the code upload; a config-only update
    (no package declared) has nothing to protect."""
    handler = LambdaFunctionHandler()
    real = boto3.client("lambda", region_name=TEST_REGION)
    created = LambdaFunction("f", function_name="fn", role_arn=_role_arn(), code_path=package)
    prior = handler.create(real, created)

    client = _Spy(real)
    config_only = LambdaFunction("f", function_name="fn", role_arn=created.role_arn)
    handler.update(client, prior, config_only)

    assert "update_function_code" not in client.calls
    assert not any(call.startswith("waiter:") for call in client.calls)
