"""SNS handlers: topics and subscriptions."""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError
from typing_extensions import override

from atlantide.providers.aws.handlers.base import (
    AwsHandler,
    is_missing,
    sync_tags,
    tag_list,
    tags_from_list,
)
from atlantide.providers.aws.handlers.faults import not_found
from atlantide.providers.aws.handlers.pagination import token_pages
from atlantide.providers.aws.resources import SnsSubscription, SnsTopic


class SnsTopicHandler(AwsHandler[SnsTopic]):
    service = "sns"
    resource_type = SnsTopic

    @override
    def create(self, client: Any, res: SnsTopic) -> dict[str, Any]:
        resp = client.create_topic(Name=res.name, Tags=tag_list(res.tags))
        return {"arn": resp["TopicArn"]}

    @override
    def read(self, client: Any, res: SnsTopic) -> dict[str, Any] | None:
        arn = _topic_arn(client, res.name)
        if arn is None:
            return None
        return {
            "arn": arn,
            "tags": tags_from_list(client.list_tags_for_resource(ResourceArn=arn).get("Tags", [])),
        }

    @override
    def update(self, client: Any, prior: dict[str, Any], res: SnsTopic) -> dict[str, Any]:
        arn = _topic_arn(client, res.name)
        if arn is None:  # update runs only on an existing topic
            raise not_found(res, "update", f"(topic {res.name!r})")
        sync_tags(
            res.tags,
            live=lambda: tags_from_list(
                client.list_tags_for_resource(ResourceArn=arn).get("Tags", [])
            ),
            untag=lambda stale, _: client.untag_resource(ResourceArn=arn, TagKeys=stale),
            tag=lambda tags: client.tag_resource(ResourceArn=arn, Tags=tag_list(tags)),
        )
        return {"arn": arn}

    @override
    def delete(self, client: Any, res: SnsTopic) -> None:
        arn = _topic_arn(client, res.name)
        if arn is not None:
            client.delete_topic(TopicArn=arn)


def _topic_arn(client: Any, name: str) -> str | None:
    """Look up a topic ARN by name (SNS ARNs are ``...:account:name``)."""
    for topic in token_pages(client.list_topics, "Topics"):
        arn = topic["TopicArn"]
        if arn.rsplit(":", 1)[-1] == name:
            return str(arn)
    return None


class SnsSubscriptionHandler(AwsHandler[SnsSubscription]):
    service = "sns"
    resource_type = SnsSubscription

    @override
    def create(self, client: Any, res: SnsSubscription) -> dict[str, Any]:
        resp = client.subscribe(
            TopicArn=res.topic_arn,
            Protocol=res.protocol,
            Endpoint=res.endpoint,
            ReturnSubscriptionArn=True,
        )
        return {"subscription_arn": resp["SubscriptionArn"]}

    @override
    def read(self, client: Any, res: SnsSubscription) -> dict[str, Any] | None:
        arn = _subscription_arn(client, res)
        return None if arn is None else {"subscription_arn": arn}

    @override
    def update(self, client: Any, prior: dict[str, Any], res: SnsSubscription) -> dict[str, Any]:
        # Every field is immutable, so a change is a REPLACE, not an update.
        return {"subscription_arn": _subscription_arn(client, res) or ""}

    @override
    def delete(self, client: Any, res: SnsSubscription) -> None:
        arn = _subscription_arn(client, res)
        if arn is not None and arn != "PendingConfirmation":
            client.unsubscribe(SubscriptionArn=arn)


def _subscription_arn(client: Any, res: SnsSubscription) -> str | None:
    """Find a subscription ARN by (topic, protocol, endpoint).

    A topic deleted out of band makes the listing raise ``NotFound``; its
    subscriptions died with it, so that is absence, not an error — the same
    ``None`` every other handler's read reports for a missing resource.
    """
    subs = token_pages(client.list_subscriptions_by_topic, "Subscriptions", TopicArn=res.topic_arn)
    try:
        for sub in subs:
            if sub["Protocol"] == res.protocol and sub["Endpoint"] == res.endpoint:
                return str(sub["SubscriptionArn"])
    except ClientError as exc:
        if not is_missing(exc):
            raise
    return None
