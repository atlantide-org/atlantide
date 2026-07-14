"""CloudWatch Logs handler: log groups."""

from __future__ import annotations

from typing import Any

from atlantide.core.errors import ProviderError
from atlantide.providers.aws.handlers.base import AwsHandler, ignore_missing
from atlantide.providers.aws.resources import CloudWatchLogGroup


class CloudWatchLogGroupHandler(AwsHandler[CloudWatchLogGroup]):
    service = "logs"
    resource_type = CloudWatchLogGroup

    def create(self, client: Any, res: CloudWatchLogGroup) -> dict[str, Any]:
        client.create_log_group(logGroupName=res.log_group_name, tags=res.tags or {})
        client.put_retention_policy(
            logGroupName=res.log_group_name, retentionInDays=res.retention_days
        )
        return self._require_outputs(client, res, "create")

    def read(self, client: Any, res: CloudWatchLogGroup) -> dict[str, Any] | None:
        return self._outputs(client, res)

    def update(
        self, client: Any, prior: dict[str, Any], res: CloudWatchLogGroup
    ) -> dict[str, Any]:
        client.put_retention_policy(
            logGroupName=res.log_group_name, retentionInDays=res.retention_days
        )
        if res.tags:
            client.tag_log_group(logGroupName=res.log_group_name, tags=res.tags)
        return self._require_outputs(client, res, "update")

    def delete(self, client: Any, res: CloudWatchLogGroup) -> None:
        with ignore_missing():
            client.delete_log_group(logGroupName=res.log_group_name)

    def _require_outputs(
        self, client: Any, res: CloudWatchLogGroup, op: str
    ) -> dict[str, Any]:
        """Outputs of a log group that must exist (it was just created/updated)."""
        outputs = self._outputs(client, res)
        if outputs is None:
            raise ProviderError(
                f"log group {res.log_group_name!r} not visible after {op}",
                op=op, resource_type=res.type_name(),
            )
        return outputs

    @staticmethod
    def _outputs(client: Any, res: CloudWatchLogGroup) -> dict[str, Any] | None:
        resp = client.describe_log_groups(logGroupNamePrefix=res.log_group_name)
        for group in resp.get("logGroups", []):
            if group["logGroupName"] == res.log_group_name:
                return {"arn": group["arn"]}
        return None
