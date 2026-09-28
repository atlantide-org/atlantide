"""SNS topics and subscriptions."""

from __future__ import annotations

import boto3
import pytest

from atlantide.core import Context
from atlantide.providers.aws import AwsProvider, SnsSubscription, SnsTopic, SqsQueue

pytestmark = pytest.mark.usefixtures("aws_env")


async def test_sns_topic_and_subscription() -> None:
    provider = AwsProvider()
    topic_out = await provider.create(Context(), SnsTopic("t", name="events", tags={"a": "1"}))
    assert topic_out["arn"].endswith(":events")
    queue_out = await provider.create(Context(), SqsQueue("q", queue_name="events-q"))

    sub = SnsSubscription("s", topic_arn=topic_out["arn"], endpoint=queue_out["arn"])
    sub_out = await provider.create(Context(), sub)
    assert sub_out["subscription_arn"].startswith("arn:aws:sns:")
    assert await provider.read(Context(), sub) is not None

    await provider.delete(Context(), sub)
    assert await provider.read(Context(), sub) is None


async def test_sns_read_missing_is_none() -> None:
    provider = AwsProvider()
    assert await provider.read(Context(), SnsTopic("t", name="ghost")) is None


async def test_sns_subscription_survives_its_topic_deleted_out_of_band() -> None:
    """Listing subscriptions of a deleted topic raises NotFound. That is absence
    (the subscriptions died with the topic), so read reports None and delete is
    a no-op instead of failing the apply."""
    provider = AwsProvider()
    topic_out = await provider.create(Context(), SnsTopic("t", name="doomed"))
    queue_out = await provider.create(Context(), SqsQueue("q", queue_name="doomed-q"))
    sub = SnsSubscription("s", topic_arn=topic_out["arn"], endpoint=queue_out["arn"])
    await provider.create(Context(), sub)

    boto3.client("sns").delete_topic(TopicArn=topic_out["arn"])

    assert await provider.read(Context(), sub) is None
    await provider.delete(Context(), sub)  # idempotent, like every other handler
