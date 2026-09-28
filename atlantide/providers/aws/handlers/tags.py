"""Tag translation and syncing shared by every AWS handler.

AWS tagging is additive and each service spells it differently; this module
holds the shapes common to all of them.
"""

from __future__ import annotations

from collections.abc import Callable


def tag_list(tags: dict[str, str]) -> list[dict[str, str]]:
    """AWS ``[{"Key": k, "Value": v}]`` tag shape, deterministically ordered."""
    return [{"Key": k, "Value": v} for k, v in sorted(tags.items())]


def tags_from_list(items: list[dict[str, str]]) -> dict[str, str]:
    """The inverse of :func:`tag_list`."""
    return {item["Key"]: item["Value"] for item in items}


def stale_tag_keys(live: dict[str, str], desired: dict[str, str]) -> list[str]:
    """Tag keys present on the live resource that config no longer declares.

    AWS tagging APIs are additive, so removed tags need an explicit untag.
    """
    return sorted(set(live) - set(desired))


def sync_tags(
    desired: dict[str, str],
    *,
    live: Callable[[], dict[str, str]],
    untag: Callable[[list[str], dict[str, str]], None],
    tag: Callable[[dict[str, str]], None],
) -> None:
    """Make a resource's tags match ``desired``, removing the ones it dropped.

    Services differ in id keyword, method names, tag shape and untag arguments,
    so the three calls come from the handler. ``untag`` receives the stale keys
    and the live tags because some APIs (ACM) take full tag objects.
    """
    current = live()
    if stale := stale_tag_keys(current, desired):
        untag(stale, current)
    if desired:
        tag(desired)
