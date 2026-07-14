"""Observability resources: CloudWatch Logs log group."""

from __future__ import annotations

from atlantide.core import computed, immutable, mutable
from atlantide.providers.aws.resources.base import AwsResource


class CloudWatchLogGroup(AwsResource):
    """A CloudWatch Logs log group.

    ``log_group_name`` and ``region`` are immutable; ``retention_days`` and
    ``tags`` update in place. ``arn`` is computed.
    """

    class Action:
        """IAM action constants, e.g. ``allow(CloudWatchLogGroup.Action.PutLogEvents, on=...)``."""

        CreateLogStream = "logs:CreateLogStream"
        PutLogEvents = "logs:PutLogEvents"

    log_group_name: str = immutable()
    retention_days: int = mutable(default=14)
    region: str = immutable()  # required (from the stack region)
    tags: dict[str, str] = mutable(default_factory=dict)
    arn: str = computed()
