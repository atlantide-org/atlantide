"""SQS queue resource."""

from __future__ import annotations

import re

from pydantic import model_validator

from atlantide.core import computed, immutable, mutable
from atlantide.providers.aws import validate as v
from atlantide.providers.aws.resources.base import AwsResource

_SQS_BASE_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


def _queue_name_rule(*, fifo: bool) -> v.Validator:
    """Validate the effective name, including the ``.fifo`` suffix for FIFO queues."""

    def run(name: str) -> str | None:
        effective = name if name.endswith(".fifo") or not fifo else f"{name}.fifo"
        base = effective.removesuffix(".fifo")
        if not _SQS_BASE_NAME.match(base):
            return f"invalid SQS queue name {name!r}: only alphanumeric, hyphens, underscores"
        if len(effective) > 80:
            return f"SQS queue name {effective!r} exceeds the 80-character limit"
        return None

    return run


class SqsQueue(AwsResource):
    """An SQS queue.

    ``queue_name``, ``region`` and ``fifo`` are immutable (a change replaces the
    queue); ``tags`` update in place. ``url`` and ``arn`` are computed outputs.
    """

    class Action:
        """IAM action constants, e.g. ``allow(SqsQueue.Action.SendMessage, on=...)``."""

        SendMessage = "sqs:SendMessage"
        ReceiveMessage = "sqs:ReceiveMessage"
        DeleteMessage = "sqs:DeleteMessage"
        GetQueueUrl = "sqs:GetQueueUrl"
        GetQueueAttributes = "sqs:GetQueueAttributes"
        PurgeQueue = "sqs:PurgeQueue"

    queue_name: str = immutable(physical_name=True)
    region: str = immutable()  # required (from the stack region)
    fifo: bool = immutable(default=False)
    tags: dict[str, str] = mutable(default_factory=dict)
    url: str = computed()
    arn: str = computed()

    @model_validator(mode="after")
    def _validate(self) -> SqsQueue:
        v.check(self.queue_name, _queue_name_rule(fifo=self.fifo))
        return self
