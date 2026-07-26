"""Observability resources: CloudWatch Logs log group."""

from __future__ import annotations

from atlantide.core import computed, immutable, mutable
from atlantide.providers.aws.resources.base import RegionalResource, TaggedResource


class CloudWatchLogGroup(RegionalResource, TaggedResource):
    """A CloudWatch Logs log group.

    ``log_group_name`` and ``region`` are immutable; ``retention_days`` and
    ``tags`` update in place. ``arn`` is computed.
    """

    class Action:
        """IAM action constants, e.g. ``allow(CloudWatchLogGroup.Action.PutLogEvents, on=...)``."""

        CreateLogStream = "logs:CreateLogStream"
        PutLogEvents = "logs:PutLogEvents"

    log_group_name: str = immutable(physical_name=True)
    retention_days: int = mutable(default=14)
    arn: str = computed()
