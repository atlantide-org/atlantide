"""Iterators over the two AWS listing pagination protocols.

An item past the first page must not read as absent: refresh would classify the
node MISSING and ``refresh --write`` would drop its state row.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from atlantide.core.errors import ProviderError


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

    CloudFront wraps items in a named envelope (``DistributionList``,
    ``OriginAccessControlList``) that also carries the truncation flags.
    """
    marker: str | None = None
    while True:
        page = call(**kwargs, **({"Marker": marker} if marker else {}))
        listing = page.get(envelope, {})
        yield from listing.get("Items", [])
        if not listing.get("IsTruncated"):
            return
        # Without a marker the next call would re-fetch the first page forever;
        # stopping instead would hide later items as absent (see module docstring).
        marker = listing.get("NextMarker")
        if not marker:
            raise ProviderError(f"{envelope} is truncated but carries no NextMarker")
