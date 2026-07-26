"""CloudWatch Logs handler: log groups."""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError
from typing_extensions import override

from atlantide.core.errors import ProviderError
from atlantide.providers.aws.handlers.base import (
    AwsHandler,
    error_code,
    ignore_missing,
    sync_tags,
)
from atlantide.providers.aws.resources import CloudWatchLogGroup


class CloudWatchLogGroupHandler(AwsHandler[CloudWatchLogGroup]):
    service = "logs"
    resource_type = CloudWatchLogGroup

    @override
    def create(self, client: Any, res: CloudWatchLogGroup) -> dict[str, Any]:
        # Adopt on re-run: a process killed between the AWS call and the state
        # persist re-runs this create against a group that already exists, and
        # every sibling handler adopts rather than failing hard.
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
    def read(self, client: Any, res: CloudWatchLogGroup) -> dict[str, Any] | None:
        group = self._find(client, res.log_group_name)
        if group is None:
            return None
        # Observe the mutable inputs as well as the arn, so refresh detects a
        # retention policy or tags edited out of band instead of reporting an
        # unchecked "in sync". "Never expire" omits the key; report None (the
        # live truth) rather than echoing the desired value as though observed.
        return {
            "arn": group["arn"],
            "retention_days": group.get("retentionInDays"),
            "tags": client.list_tags_log_group(logGroupName=res.log_group_name).get("tags", {}),
        }

    @override
    def update(self, client: Any, prior: dict[str, Any], res: CloudWatchLogGroup) -> dict[str, Any]:
        client.put_retention_policy(
            logGroupName=res.log_group_name, retentionInDays=res.retention_days
        )
        # CloudWatch Logs is the odd one: lowercase `tags`, and an untag that takes
        # the keys under that same keyword rather than `TagKeys`.
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
    def delete(self, client: Any, res: CloudWatchLogGroup) -> None:
        with ignore_missing():
            client.delete_log_group(logGroupName=res.log_group_name)

    def _require_outputs(self, client: Any, res: CloudWatchLogGroup, op: str) -> dict[str, Any]:
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
    def _outputs(client: Any, res: CloudWatchLogGroup) -> dict[str, Any] | None:
        group = CloudWatchLogGroupHandler._find(client, res.log_group_name)
        return {"arn": group["arn"]} if group is not None else None

    @staticmethod
    def _find(client: Any, name: str) -> dict[str, Any] | None:
        """The log group named exactly ``name``, searching every page.

        ``describe_log_groups`` filters by *prefix* and returns 50 per page, so a
        single request only finds the group when fewer than 50 others share its
        prefix — a condition no config controls and nothing warns about. Missing
        it does not degrade gracefully: the read returns ``None``, refresh
        classifies the node MISSING, and ``refresh --write`` deletes the state row
        for a log group that is sitting there perfectly healthy.
        """
        pages = client.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=name)
        for page in pages:
            for group in page.get("logGroups", []):
                if group["logGroupName"] == name:
                    return dict(group)
        return None
