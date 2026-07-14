"""Messaging resources: SNS topic and subscription."""

from __future__ import annotations

from atlantide.core import computed, immutable, mutable
from atlantide.providers.aws.resources.base import AwsResource


class SnsTopic(AwsResource):
    """An SNS topic. ``name``/``region`` immutable; ``tags`` in place; ``arn`` computed."""

    class Action:
        """IAM action constants, e.g. ``allow(SnsTopic.Action.Publish, on=...)``."""

        Publish = "sns:Publish"
        Subscribe = "sns:Subscribe"

    name: str = immutable(physical_name=True)
    region: str = immutable()  # required (from the stack region)
    tags: dict[str, str] = mutable(default_factory=dict)
    arn: str = computed()


class SnsSubscription(AwsResource):
    """An SNS subscription wiring a topic to an endpoint (e.g. an SQS queue).

    All fields are immutable — any change replaces the subscription. Pass
    ``topic.arn`` and ``queue.arn`` so the subscription depends on both.
    ``subscription_arn`` is computed.
    """

    topic_arn: str = immutable()
    protocol: str = immutable(default="sqs")
    endpoint: str = immutable()
    region: str = immutable()  # required (from the stack region)
    subscription_arn: str = computed()
