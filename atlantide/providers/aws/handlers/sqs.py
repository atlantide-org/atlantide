"""SQS handler: queues."""

from __future__ import annotations

from typing import Any

from atlantide.core.errors import ProviderError
from atlantide.providers.aws.handlers.base import AwsHandler
from atlantide.providers.aws.resources import SqsQueue


class SqsQueueHandler(AwsHandler[SqsQueue]):
    service = "sqs"
    resource_type = SqsQueue

    def create(self, client: Any, res: SqsQueue) -> dict[str, Any]:
        attributes = {"FifoQueue": "true"} if res.fifo else {}
        resp = client.create_queue(
            QueueName=_queue_name(res), Attributes=attributes, tags=res.tags or {}
        )
        return self._outputs(client, resp["QueueUrl"])

    def read(self, client: Any, res: SqsQueue) -> dict[str, Any] | None:
        url = self._url(client, res)
        return None if url is None else self._outputs(client, url)

    def update(self, client: Any, prior: dict[str, Any], res: SqsQueue) -> dict[str, Any]:
        url = self._url(client, res)
        if url is None:  # update runs only on an existing queue
            raise ProviderError(
                f"queue {_queue_name(res)!r} not found",
                op="update", resource_type=res.type_name(),
            )
        if res.tags:
            client.tag_queue(QueueUrl=url, Tags=res.tags)
        return self._outputs(client, url)

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


def _queue_name(res: SqsQueue) -> str:
    """Queue name; FIFO queues must end in ``.fifo``."""
    if res.fifo and not res.queue_name.endswith(".fifo"):
        return f"{res.queue_name}.fifo"
    return res.queue_name
