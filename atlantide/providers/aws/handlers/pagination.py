"""The two AWS listing protocols, so no handler hand-rolls a paging loop.

Reading only the first page is a silent correctness bug, not a performance one:
an item past page one reads as absent, refresh classifies the node MISSING, and
``refresh --write`` drops the state row of a healthy resource. Both protocols
live here so that cannot be got wrong per handler.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any


def token_pages(call: Any, key: str, **kwargs: Any) -> Iterator[dict[str, Any]]:
    """Every item across a ``NextToken``-paged listing (SNS, SQS, Lambda...)."""
    token: str | None = None
    while True:
        page = call(**kwargs, **({"NextToken": token} if token else {}))
        yield from page.get(key, [])
        token = page.get("NextToken")
        if not token:
            return


def marker_pages(call: Any, envelope: str, **kwargs: Any) -> Iterator[dict[str, Any]]:
    """Every item across a ``Marker``/``NextMarker``-paged listing (CloudFront).

    CloudFront wraps its items in a named envelope (``DistributionList``,
    ``OriginAccessControlList``) that also carries the truncation flags.
    """
    marker: str | None = None
    while True:
        page = call(**kwargs, **({"Marker": marker} if marker else {}))
        listing = page.get(envelope, {})
        yield from listing.get("Items", [])
        if not listing.get("IsTruncated"):
            return
        marker = listing.get("NextMarker")
