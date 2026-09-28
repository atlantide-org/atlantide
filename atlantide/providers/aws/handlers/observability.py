"""CloudWatch Logs handler: log groups."""

from __future__ import annotations

from typing import Any, override

from botocore.exceptions import ClientError

from atlantide.core.errors import ProviderError
from atlantide.providers.aws.handlers.base import (
    AwsHandler,
    Client,
    error_code,
    ignore_missing,
    sync_tags,
)
from atlantide.providers.aws.resources import CloudWatchLogGroup


class CloudWatchLogGroupHandler(AwsHandler[CloudWatchLogGroup]):
    service = "logs"
    resource_type = CloudWatchLogGroup

    @override
    def create(self, client: Client, res: CloudWatchLogGroup) -> dict[str, Any]:
        # Adopt an existing group: a process killed between the AWS call and the
        # state write re-runs this create.
        try:
            client.create_log_group(logGroupName=res.log_group_name, tags=res.tags or {})
        except ClientError as exc:
            if error_code(exc) != "ResourceAlreadyExistsException":
                raise
        client.put_retention_policy(
            logGroupName=res.log_group_name, retentionInDays=res.retention_days
        )
        return self._require_outputs(client, res, "create")

    @override
    def read(self, client: Client, res: CloudWatchLogGroup) -> dict[str, Any] | None:
        group = self._find(client, res.log_group_name)
        if group is None:
            return None
        # Report retention and tags so refresh detects out-of-band edits. A group
        # that never expires has no ``retentionInDays``, so retention reports None.
        return {
            "arn": group["arn"],
            "retention_days": group.get("retentionInDays"),
            "tags": client.list_tags_log_group(logGroupName=res.log_group_name).get("tags", {}),
        }

    @override
    def update(
        self, client: Client, prior: dict[str, Any], res: CloudWatchLogGroup
    ) -> dict[str, Any]:
        client.put_retention_policy(
            logGroupName=res.log_group_name, retentionInDays=res.retention_days
        )
        # CloudWatch Logs uses lowercase `tags`, and its untag takes the keys under
        # that same keyword rather than `TagKeys`.
        sync_tags(
            res.tags,
            live=lambda: client.list_tags_log_group(logGroupName=res.log_group_name).get(
                "tags", {}
            ),
            untag=lambda stale, _: client.untag_log_group(
                logGroupName=res.log_group_name, tags=stale
            ),
            tag=lambda tags: client.tag_log_group(logGroupName=res.log_group_name, tags=tags),
        )
        return self._require_outputs(client, res, "update")

    @override
    def delete(self, client: Client, res: CloudWatchLogGroup) -> None:
        with ignore_missing():
            client.delete_log_group(logGroupName=res.log_group_name)

    def _require_outputs(self, client: Client, res: CloudWatchLogGroup, op: str) -> dict[str, Any]:
        """Outputs of a log group that must exist (it was just created/updated)."""
        outputs = self._outputs(client, res)
        if outputs is None:
            raise ProviderError(
                f"log group {res.log_group_name!r} not visible after {op}",
                op=op,
                resource_type=res.type_name(),
            )
        return outputs

    @staticmethod
    def _outputs(client: Client, res: CloudWatchLogGroup) -> dict[str, Any] | None:
        group = CloudWatchLogGroupHandler._find(client, res.log_group_name)
        return {"arn": group["arn"]} if group is not None else None

    @staticmethod
    def _find(client: Client, name: str) -> dict[str, Any] | None:
        """The log group named exactly ``name``, searching every page.

        ``describe_log_groups`` filters by prefix and returns 50 groups per page,
        so a single request can miss the group. A missed group reads as ``None``,
        which refresh classifies as MISSING.
        """
        pages = client.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=name)
        for page in pages:
            for group in page.get("logGroups", []):
                if group["logGroupName"] == name:
                    return dict(group)
        return None
