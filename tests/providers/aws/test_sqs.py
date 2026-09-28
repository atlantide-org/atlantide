"""SQS queues: CRUD, FIFO naming, adoption, and queue attributes."""

from __future__ import annotations

import boto3
import pytest

from atlantide.core import Context
from atlantide.providers.aws import AwsProvider, SqsQueue
from tests.support import TEST_REGION

pytestmark = pytest.mark.usefixtures("aws_env")


async def test_sqs_create_read_update_delete() -> None:
    provider = AwsProvider()
    res = SqsQueue("q", queue_name="jobs", tags={"team": "infra"})
    out = await provider.create(Context(), res)
    assert out["url"].endswith("/jobs")
    assert out["arn"].endswith(":jobs")

    assert await provider.read(Context(), res) is not None

    await provider.update(Context(), out, SqsQueue("q", queue_name="jobs", tags={"team": "ops"}))
    client = boto3.client("sqs")
    tags = client.list_queue_tags(QueueUrl=out["url"]).get("Tags", {})
    assert tags["team"] == "ops"

    await provider.delete(Context(), res)
    assert await provider.read(Context(), res) is None


async def test_sqs_fifo_queue() -> None:
    provider = AwsProvider()
    out = await provider.create(Context(), SqsQueue("q", queue_name="events.fifo", fifo=True))
    attrs = boto3.client("sqs").get_queue_attributes(
        QueueUrl=out["url"], AttributeNames=["FifoQueue"]
    )
    assert attrs["Attributes"]["FifoQueue"] == "true"


async def test_sqs_fifo_name_gets_suffix() -> None:
    # AWS requires FIFO names to end in .fifo; the provider appends it, and
    # read/delete look the queue up under the same suffixed name.
    provider = AwsProvider()
    res = SqsQueue("q", queue_name="events", fifo=True)  # no .fifo suffix
    out = await provider.create(Context(), res)
    assert out["url"].endswith("/events.fifo")
    assert await provider.read(Context(), res) is not None  # found under events.fifo
    await provider.delete(Context(), res)
    assert await provider.read(Context(), res) is None


async def test_sqs_read_missing_is_none() -> None:
    provider = AwsProvider()
    assert await provider.read(Context(), SqsQueue("q", queue_name="nope")) is None


async def test_sqs_create_with_changed_attributes_adopts_the_existing_queue() -> None:
    """A re-run create (state row never persisted) whose attributes differ from
    the live queue's answers QueueAlreadyExists; it adopts by name rather than
    failing the apply."""
    provider = AwsProvider()
    out = await provider.create(Context(), SqsQueue("q", queue_name="jobs", visibility_timeout=30))
    again = await provider.create(
        Context(), SqsQueue("q", queue_name="jobs", visibility_timeout=60)
    )
    assert again == out


# -- dead-letter target and attributes -------------------------------------------


def test_a_queue_declares_a_dead_letter_target() -> None:
    """Without one, a message that always fails is redelivered forever and blocks
    everything behind it."""
    from atlantide.providers.aws import SqsQueue
    from atlantide.providers.aws.handlers.sqs import _attributes

    attributes = _attributes(
        SqsQueue(
            "q",
            queue_name="work",
            region=TEST_REGION,
            dead_letter_target_arn="arn:aws:sqs:eu-north-1:1:dlq",
            max_receive_count=3,
            visibility_timeout=60,
            receive_wait_time_seconds=20,
        )
    )

    import json as _json

    redrive = _json.loads(attributes["RedrivePolicy"])
    assert redrive["maxReceiveCount"] == 3
    assert redrive["deadLetterTargetArn"].endswith(":dlq")
    assert attributes["VisibilityTimeout"] == "60"
    assert attributes["ReceiveMessageWaitTimeSeconds"] == "20"


async def test_queue_attributes_are_applied_on_create() -> None:
    from atlantide.providers.aws import SqsQueue

    provider = AwsProvider()
    queue = SqsQueue("q", queue_name="tuned", region=TEST_REGION, visibility_timeout=45)
    out = await provider.create(Context(), queue)

    live = boto3.client("sqs", region_name=TEST_REGION).get_queue_attributes(
        QueueUrl=out["url"], AttributeNames=["VisibilityTimeout"]
    )["Attributes"]
    assert live["VisibilityTimeout"] == "45"
