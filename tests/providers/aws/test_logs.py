"""CloudWatch log groups: CRUD, paging, and what a read reports."""

from __future__ import annotations

from typing import Any

import boto3
import pytest

from atlantide.core import Context
from atlantide.providers.aws import AwsProvider, CloudWatchLogGroup
from atlantide.providers.aws.handlers.observability import CloudWatchLogGroupHandler

pytestmark = pytest.mark.usefixtures("aws_env")


async def test_cloudwatch_log_group_crud() -> None:
    provider = AwsProvider()
    res = CloudWatchLogGroup("l", log_group_name="/svc/app", retention_days=7)
    out = await provider.create(Context(), res)
    assert out["arn"].startswith("arn:aws:logs:")
    groups = boto3.client("logs").describe_log_groups(logGroupNamePrefix="/svc/app")["logGroups"]
    assert groups[0]["retentionInDays"] == 7

    await provider.update(
        Context(), out, CloudWatchLogGroup("l", log_group_name="/svc/app", retention_days=30)
    )
    groups = boto3.client("logs").describe_log_groups(logGroupNamePrefix="/svc/app")["logGroups"]
    assert groups[0]["retentionInDays"] == 30

    await provider.delete(Context(), res)
    assert await provider.read(Context(), res) is None


def test_a_log_group_is_found_past_the_first_page() -> None:
    """`describe_log_groups` filters by prefix and pages at 50.

    A single unpaginated request finds the group only when fewer than 50 others
    share its prefix. Otherwise `read` returns None, refresh reports the node
    MISSING, and `refresh --write` deletes the state row of a live log group.

    Uses a stub rather than moto, which does not enforce the page limit and so
    would pass whether or not the handler paginates.
    """

    class PagingLogs:
        """A `logs` client that pages the way the real API does."""

        def __init__(self) -> None:
            first = [{"logGroupName": f"/svc/shared-{i:03d}", "arn": "a"} for i in range(50)]
            second = [{"logGroupName": "/svc/shared-target", "arn": "arn::target"}]
            self.pages = [{"logGroups": first}, {"logGroups": second}]

        def describe_log_groups(self, **_kw: Any) -> dict[str, Any]:
            return self.pages[0]  # one request only ever sees page one

        def get_paginator(self, _name: str) -> Any:
            pages = self.pages

            class Paginator:
                def paginate(self, **_kw: Any) -> Any:
                    return iter(pages)

            return Paginator()

    found = CloudWatchLogGroupHandler._find(PagingLogs(), "/svc/shared-target")

    assert found is not None, "the group exists but was not found past page one"
    assert found["arn"] == "arn::target"


async def test_a_log_group_read_reports_the_inputs_it_can_check() -> None:
    """A read returning only the arn makes refresh say "in sync" about a retention
    policy it never looked at."""
    provider = AwsProvider()
    res = CloudWatchLogGroup(
        "l", log_group_name="/svc/observed", retention_days=14, tags={"env": "prod"}
    )
    await provider.create(Context(), res)

    live = await provider.read(Context(), res)

    assert live is not None
    assert live["retention_days"] == 14
    assert live["tags"] == {"env": "prod"}


async def test_a_log_group_that_is_really_gone_still_reads_as_missing() -> None:
    """Pagination must not make an absent log group read as present."""
    provider = AwsProvider()
    res = CloudWatchLogGroup("l", log_group_name="/svc/never-made", retention_days=7)
    assert await provider.read(Context(), res) is None
