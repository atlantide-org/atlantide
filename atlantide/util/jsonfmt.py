"""Compact, key-sorted JSON.

Used for every byte-stable JSON the engine hashes or compares: state documents
(S3 compare-and-swap depends on the exact bytes), the ``to_json`` builtin, and
IAM policy documents. Changing the separators or key order changes all of them.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["compact_json"]


def compact_json(value: Any, *, ascii: bool = True) -> str:
    """``value`` as JSON with sorted keys and no insignificant whitespace.

    ``ascii=True`` escapes non-ASCII characters (``\\u00e9``); ``False`` writes
    them as-is.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=ascii)
