"""SQS handler: queues."""

from __future__ import annotations

import contextlib
import json
from typing import Any

from typing_extensions import override

from atlantide.providers.aws.handlers.base import (
    AwsHandler,
    create_or_adopt,
    sync_tags,
)
from atlantide.providers.aws.handlers.faults import not_found
from atlantide.providers.aws.resources import SqsQueue


class SqsQueueHandler(AwsHandler[SqsQueue]):
    service = "sqs"
    resource_type = SqsQueue

    @override
    def create(self, client: Any, res: SqsQueue) -> dict[str, Any]:
        def make() -> dict[str, Any]:
            resp = client.create_queue(
                QueueName=_queue_name(res), Attributes=_attributes(res), tags=res.tags or {}
            )
            return self._outputs(client, resp["QueueUrl"])

        def read() -> dict[str, Any] | None:
            url = self._url(client, res)
            return None if url is None else self._outputs(client, url)

        # A re-run create whose attributes differ from the live queue's answers
        # QueueAlreadyExists rather than returning the URL; adopt by name, as
        # every other named-resource handler does.
        return create_or_adopt(make, read)

    @override
    def read(self, client: Any, res: SqsQueue) -> dict[str, Any] | None:
        url = self._url(client, res)
        if url is None:
            return None
        live = client.get_queue_attributes(QueueUrl=url, AttributeNames=["All"]).get(
            "Attributes", {}
        )
        # SQS reports every attribute as a string, so the numeric ones are
        # converted back — otherwise every comparison sees "30" against 30 and
        # reports drift that is not there.
        observed: dict[str, Any] = dict(self._outputs(client, url))
        for field, key in (
            ("visibility_timeout", "VisibilityTimeout"),
            ("message_retention_seconds", "MessageRetentionPeriod"),
            ("receive_wait_time_seconds", "ReceiveMessageWaitTimeSeconds"),
        ):
            if key in live:
                observed[field] = int(live[key])
        if (policy := live.get("RedrivePolicy")) is not None:
            with contextlib.suppress(ValueError):
                parsed = json.loads(policy)
                observed["dead_letter_target_arn"] = parsed.get("deadLetterTargetArn")
                observed["max_receive_count"] = int(parsed.get("maxReceiveCount", 0))
        observed["tags"] = client.list_queue_tags(QueueUrl=url).get("Tags", {})
        return observed

    @override
    def update(self, client: Any, prior: dict[str, Any], res: SqsQueue) -> dict[str, Any]:
        url = self._url(client, res)
        if url is None:  # update runs only on an existing queue
            raise not_found(res, "update", f"(queue {_queue_name(res)!r})")
        # `FifoQueue` is immutable, so it is excluded here: sending it again is
        # an error even when the value is unchanged.
        mutable_attributes = {
            key: value for key, value in _attributes(res).items() if key != "FifoQueue"
        }
        # SQS leaves an *omitted* attribute untouched, so removing the redrive
        # policy or KMS key from config can never converge unless the empty
        # string is sent explicitly to clear it.
        if res.dead_letter_target_arn is None:
            mutable_attributes["RedrivePolicy"] = ""
        if res.kms_key_id is None:
            mutable_attributes["KmsMasterKeyId"] = ""
        client.set_queue_attributes(QueueUrl=url, Attributes=mutable_attributes)
        sync_tags(
            res.tags,
            live=lambda: client.list_queue_tags(QueueUrl=url).get("Tags", {}),
            untag=lambda stale, _: client.untag_queue(QueueUrl=url, TagKeys=stale),
            tag=lambda tags: client.tag_queue(QueueUrl=url, Tags=tags),
        )
        return self._outputs(client, url)

    @override
    def delete(self, client: Any, res: SqsQueue) -> None:
        url = self._url(client, res)
        if url is not None:
            client.delete_queue(QueueUrl=url)

    @staticmethod
    def _url(client: Any, res: SqsQueue) -> str | None:
        try:
            return str(client.get_queue_url(QueueName=_queue_name(res))["QueueUrl"])
        except client.exceptions.QueueDoesNotExist:
            return None

    @staticmethod
    def _outputs(client: Any, url: str) -> dict[str, Any]:
        attrs = client.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])
        return {"url": url, "arn": attrs["Attributes"]["QueueArn"]}


def _attributes(res: SqsQueue) -> dict[str, str]:
    """The queue's settings, in SQS's all-strings attribute map."""
    attributes: dict[str, str] = {
        "VisibilityTimeout": str(res.visibility_timeout),
        "MessageRetentionPeriod": str(res.message_retention_seconds),
        "ReceiveMessageWaitTimeSeconds": str(res.receive_wait_time_seconds),
    }
    if res.fifo:
        attributes["FifoQueue"] = "true"
    if res.kms_key_id is not None:
        attributes["KmsMasterKeyId"] = res.kms_key_id
    if res.dead_letter_target_arn is not None:
        # A JSON string inside the attribute map, which is SQS's shape rather
        # than ours.
        attributes["RedrivePolicy"] = json.dumps(
            {
                "deadLetterTargetArn": res.dead_letter_target_arn,
                "maxReceiveCount": res.max_receive_count,
            }
        )
    return attributes


def _queue_name(res: SqsQueue) -> str:
    """Queue name; FIFO queues must end in ``.fifo``."""
    if res.fifo and not res.queue_name.endswith(".fifo"):
        return f"{res.queue_name}.fifo"
    return res.queue_name
