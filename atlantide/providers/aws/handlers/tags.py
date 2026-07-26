"""Tag translation and syncing shared by every AWS handler.

AWS tagging is additive and every service spells it slightly differently; the
shapes that do not vary live here.
"""

from __future__ import annotations

from collections.abc import Callable


def tag_list(tags: dict[str, str]) -> list[dict[str, str]]:
    """AWS ``[{"Key": k, "Value": v}]`` tag shape, deterministically ordered."""
    return [{"Key": k, "Value": v} for k, v in sorted(tags.items())]


def tags_from_list(items: list[dict[str, str]]) -> dict[str, str]:
    """The inverse of :func:`tag_list`, for the services that read tags back
    as ``[{"Key": ..., "Value": ...}]``."""
    return {item["Key"]: item["Value"] for item in items}


def stale_tag_keys(live: dict[str, str], desired: dict[str, str]) -> list[str]:
    """Tag keys present on the live resource that config no longer declares.

    AWS tagging APIs are additive: a ``tag_*`` call with the remaining tags leaves
    removed ones in place, so syncing tags requires an explicit untag.
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

    Every service spells tagging differently — the id keyword, the method names,
    whether tags read back as a list or a mapping, and whether an untag takes bare
    keys or whole tag objects all vary — so the three calls stay with the handler
    that knows its own API. What does not vary is the *shape*, and getting it
    wrong is silent: because AWS tagging is additive, a handler that only calls
    ``tag`` leaves a tag the config deleted in place forever, and no plan will
    ever mention it again.

    Passing ``untag`` is therefore not optional. It receives the stale keys and
    the live tags, because a few APIs (ACM) want the full tag objects back.
    """
    current = live()
    if stale := stale_tag_keys(current, desired):
        untag(stale, current)
    if desired:
        tag(desired)
