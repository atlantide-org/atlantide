"""Plan-time input validation on the AWS resource types."""

from __future__ import annotations

import pytest

from atlantide.providers.aws import (
    DynamoDbTable,
    IamRole,
    S3Bucket,
    ServicePrincipal,
    SqsQueue,
    Subnet,
    Vpc,
)

pytestmark = pytest.mark.usefixtures("aws_env")


def test_input_validation_rejects_bad_values() -> None:
    with pytest.raises(ValueError, match="S3 bucket name"):
        S3Bucket("b", bucket="Not_A_Valid_Bucket")  # uppercase + underscore
    with pytest.raises(ValueError, match="SQS queue name"):
        SqsQueue("q", queue_name="has spaces")
    with pytest.raises(ValueError, match="80-character"):
        SqsQueue("q", queue_name="x" * 81)
    with pytest.raises(ValueError, match="CIDR"):
        Vpc("v", cidr_block="10.0.0/16")  # malformed
    with pytest.raises(ValueError, match="CIDR"):
        Subnet("s", vpc_id="vpc-1", cidr_block="10.0.0.999/24")  # octet > 255
    with pytest.raises(ValueError, match="billing_mode"):
        DynamoDbTable("d", table_name="t", hash_key="id", billing_mode="NOPE")
    with pytest.raises(ValueError, match="64-character"):
        IamRole("r", role_name="x" * 65, assumed_by=ServicePrincipal.Ec2)


def test_valid_inputs_and_refs_pass_validation() -> None:
    S3Bucket("b", bucket="atlantide-assets-dev")
    SqsQueue("q", queue_name="jobs", fifo=True)  # .fifo appended by the provider, name valid
    Vpc("v", cidr_block="10.0.0.0/16")
    # a validated field still holding a Ref is skipped (value unknown until apply)
    queue = SqsQueue("qref", queue_name="q1")
    S3Bucket("b2", bucket=queue.arn)
