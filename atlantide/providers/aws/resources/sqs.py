"""SQS queue resource."""

from __future__ import annotations

import re

from pydantic import model_validator

from atlantide.core import computed, immutable, mutable
from atlantide.providers.aws import validate as v
from atlantide.providers.aws.resources.base import RegionalResource, TaggedResource

#: ASCII only, matched against the whole name (``fullmatch``).
_SQS_BASE_NAME = re.compile(r"[A-Za-z0-9_-]+")


def _queue_name_rule(*, fifo: bool) -> v.Validator:
    """Validate the effective name, including the ``.fifo`` suffix for FIFO queues."""

    def run(name: str) -> str | None:
        if name.endswith(".fifo") and not fifo:
            return f"SQS queue name {name!r} ends in '.fifo', which requires fifo=True"
        effective = name if name.endswith(".fifo") or not fifo else f"{name}.fifo"
        base = effective.removesuffix(".fifo")
        if not _SQS_BASE_NAME.fullmatch(base):
            return f"invalid SQS queue name {name!r}: only alphanumeric, hyphens, underscores"
        if len(effective) > 80:
            return f"SQS queue name {effective!r} exceeds the 80-character limit"
        return None

    return run


class SqsQueue(RegionalResource, TaggedResource):
    """An SQS queue.

    ``queue_name``, ``region`` and ``fifo`` are immutable (a change replaces the
    queue); everything else updates in place. ``url`` and ``arn`` are computed.

    **Dead-letter queue.** ``dead_letter_target_arn`` (pass another queue's
    ``arn``) with ``max_receive_count`` stops a message that always fails from
    being redelivered indefinitely.
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
    fifo: bool = immutable(default=False)
    #: Seconds a consumer has to process a message before it reappears.
    visibility_timeout: int = mutable(default=30)
    #: Seconds an unconsumed message is kept. Four days is AWS's default.
    message_retention_seconds: int = mutable(default=345_600)
    #: Long-poll wait in seconds; 0 (the AWS default) disables long polling.
    receive_wait_time_seconds: int = mutable(default=0)
    #: Where messages go after failing ``max_receive_count`` times.
    dead_letter_target_arn: str | None = mutable(default=None)
    max_receive_count: int = mutable(default=5)
    #: KMS key for server-side encryption; SQS-managed (SSE-SQS) when unset.
    kms_key_id: str | None = mutable(default=None)
    url: str = computed()
    arn: str = computed()

    @model_validator(mode="after")
    def _validate(self) -> SqsQueue:
        if self.dead_letter_target_arn is not None and self.max_receive_count < 1:
            raise ValueError("max_receive_count must be at least 1 for a dead-letter queue")
        v.check(self.queue_name, _queue_name_rule(fifo=self.fifo))
        return self
