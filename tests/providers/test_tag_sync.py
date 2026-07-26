"""Removing a tag from config must remove it from the resource.

AWS tagging APIs are additive. A handler that reacts to a tag change by calling
only ``tag_*`` with the remaining tags leaves the deleted one in place — and then
never mentions it again, because the next refresh compares config against a read
that reports the tag the config no longer has, sees a difference, calls ``tag_*``
once more, and settles into reporting drift it cannot fix.

Nothing tested this before :func:`sync_tags` existed: every handler's tag test
added tags and asserted they arrived. Deleting the untag step from all eight
handlers kept the suite green, which is the whole reason the helper takes an
``untag`` callback rather than trusting each handler to remember one.
"""

from __future__ import annotations

from typing import Any

import pytest

from atlantide.core import Context
from atlantide.core.resource import Resource
from atlantide.providers.aws import (
    AwsProvider,
    CloudWatchLogGroup,
    DynamoDbTable,
    IamRole,
    SnsTopic,
    SqsQueue,
)
from tests.support import TEST_REGION, aws_fixture

aws_env = aws_fixture()

BOTH = {"keep": "yes", "drop": "yes"}
ONLY_KEPT = {"keep": "yes"}


def _resources(tags: dict[str, str]) -> dict[str, Resource]:
    """One resource per handler whose update path reconciles tags."""
    return {
        "aws.SnsTopic": SnsTopic("t", name="atlantide-tagsync", region=TEST_REGION, tags=tags),
        "aws.SqsQueue": SqsQueue(
            "q", queue_name="atlantide-tagsync", region=TEST_REGION, tags=tags
        ),
        "aws.DynamoDbTable": DynamoDbTable(
            "d",
            table_name="atlantide-tagsync",
            region=TEST_REGION,
            hash_key="pk",
            tags=tags,
        ),
        "aws.IamRole": IamRole(
            "r",
            role_name="atlantide-tagsync",
            assumed_by="lambda.amazonaws.com",
            tags=tags,
        ),
        "aws.CloudWatchLogGroup": CloudWatchLogGroup(
            "l",
            log_group_name="/atlantide/tagsync",
            region=TEST_REGION,
            tags=tags,
        ),
    }


@pytest.mark.parametrize("type_name", sorted(_resources({})))
async def test_a_tag_removed_from_config_is_removed_from_the_resource(type_name: str) -> None:
    """Create tagged with two, update declaring one, and require the other gone."""
    provider = AwsProvider()
    context = Context()
    tagged = _resources(BOTH)[type_name]
    outputs = await provider.create(context, tagged)

    retagged = _resources(ONLY_KEPT)[type_name]
    await provider.update(context, outputs, retagged)

    live: dict[str, Any] | None = await provider.read(context, retagged)
    assert live is not None
    assert live["tags"] == ONLY_KEPT, (
        f"{type_name} left the dropped tag in place — the untag step is missing, "
        f"and AWS tagging is additive so nothing else will ever remove it"
    )


@pytest.mark.parametrize("type_name", sorted(_resources({})))
async def test_the_kept_tags_are_not_disturbed(type_name: str) -> None:
    """The obvious failure mode of an over-eager fix: untag everything, then
    re-tag, leaving a window — or worse, untag and forget to re-tag."""
    provider = AwsProvider()
    context = Context()
    outputs = await provider.create(context, _resources(BOTH)[type_name])

    changed = _resources({"keep": "yes", "added": "new"})[type_name]
    await provider.update(context, outputs, changed)

    live = await provider.read(context, changed)
    assert live is not None
    assert live["tags"] == {"keep": "yes", "added": "new"}


async def test_clearing_every_tag_leaves_none() -> None:
    """The edge the `if desired:` guard creates: with nothing to tag, the untag
    still has to run, or an emptied tag set silently keeps all its old tags."""
    provider = AwsProvider()
    context = Context()
    outputs = await provider.create(context, _resources(BOTH)["aws.SnsTopic"])

    bare = _resources({})["aws.SnsTopic"]
    await provider.update(context, outputs, bare)

    live = await provider.read(context, bare)
    assert live is not None
    assert live["tags"] == {}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
