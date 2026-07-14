"""SNS handlers: topics and subscriptions."""

from __future__ import annotations

from typing import Any

from atlantide.core.errors import ProviderError
from atlantide.providers.aws.handlers.base import AwsHandler, tag_list
from atlantide.providers.aws.resources import SnsSubscription, SnsTopic


class SnsTopicHandler(AwsHandler[SnsTopic]):
    service = "sns"
    resource_type = SnsTopic

    def create(self, client: Any, res: SnsTopic) -> dict[str, Any]:
        resp = client.create_topic(Name=res.name, Tags=tag_list(res.tags))
        return {"arn": resp["TopicArn"]}

    def read(self, client: Any, res: SnsTopic) -> dict[str, Any] | None:
        arn = _topic_arn(client, res.name)
        return None if arn is None else {"arn": arn}

    def update(self, client: Any, prior: dict[str, Any], res: SnsTopic) -> dict[str, Any]:
        arn = _topic_arn(client, res.name)
        if arn is None:  # update runs only on an existing topic
            raise ProviderError(
                f"topic {res.name!r} not found", op="update", resource_type=res.type_name()
            )
        if res.tags:
            client.tag_resource(ResourceArn=arn, Tags=tag_list(res.tags))
        return {"arn": arn}

    def delete(self, client: Any, res: SnsTopic) -> None:
        arn = _topic_arn(client, res.name)
        if arn is not None:
            client.delete_topic(TopicArn=arn)


def _topic_arn(client: Any, name: str) -> str | None:
    """Look up a topic ARN by name (SNS ARNs are ``...:account:name``)."""
    for topic in client.list_topics().get("Topics", []):
        arn = topic["TopicArn"]
        if arn.rsplit(":", 1)[-1] == name:
            return str(arn)
    return None


class SnsSubscriptionHandler(AwsHandler[SnsSubscription]):
    service = "sns"
    resource_type = SnsSubscription

    def create(self, client: Any, res: SnsSubscription) -> dict[str, Any]:
        resp = client.subscribe(
            TopicArn=res.topic_arn,
            Protocol=res.protocol,
            Endpoint=res.endpoint,
            ReturnSubscriptionArn=True,
        )
        return {"subscription_arn": resp["SubscriptionArn"]}

    def read(self, client: Any, res: SnsSubscription) -> dict[str, Any] | None:
        arn = _subscription_arn(client, res)
        return None if arn is None else {"subscription_arn": arn}

    def update(self, client: Any, prior: dict[str, Any], res: SnsSubscription) -> dict[str, Any]:
        # every field is immutable, so a change is a REPLACE, not an update
        return {"subscription_arn": _subscription_arn(client, res) or ""}

    def delete(self, client: Any, res: SnsSubscription) -> None:
        arn = _subscription_arn(client, res)
        if arn is not None and arn != "PendingConfirmation":
            client.unsubscribe(SubscriptionArn=arn)


def _subscription_arn(client: Any, res: SnsSubscription) -> str | None:
    """Find a subscription ARN by (topic, protocol, endpoint)."""
    resp = client.list_subscriptions_by_topic(TopicArn=res.topic_arn)
    for sub in resp.get("Subscriptions", []):
        if sub["Protocol"] == res.protocol and sub["Endpoint"] == res.endpoint:
            return str(sub["SubscriptionArn"])
    return None
