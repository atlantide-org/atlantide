"""Canonical JSON encoding (RFC 8785 JCS-style).

The same logical value must always encode to the *same bytes* so ``hash(IR)`` is
a stable plan identity across runs and machines. Enforces: sorted object keys,
compact separators, UTF-8, minimal string escaping, and no NaN/Infinity.

Object keys are sorted by Unicode code point; floats use Python's shortest
``repr``.
"""

from __future__ import annotations

import json
import math
from typing import Any

from atlantide.core.errors import IRError

# Root path shown when an encode error happens at the top-level value.
_ROOT = "<root>"


def to_canonical_json(value: Any) -> bytes:
    """Encode ``value`` to canonical UTF-8 JSON bytes."""
    return _encode(value, _ROOT).encode("utf-8")


def _encode(value: Any, path: str) -> str:
    """Encode one value. ``path`` locates ``value`` in the tree for error messages."""
    if value is None:
        return "null"
    # bool is a subclass of int, so it MUST be checked before the int branch.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _encode_float(value, path)
    if isinstance(value, dict):
        return _encode_object(value, path)
    if isinstance(value, list | tuple):
        return _encode_array(value, path)
    raise IRError(f"value of type {type(value).__name__} at {path} is not JSON-encodable")


def _encode_float(value: float, path: str) -> str:
    if math.isnan(value) or math.isinf(value):
        raise IRError(f"non-finite float {value!r} at {path} is not encodable")
    return repr(value)


def _encode_array(items: list[Any] | tuple[Any, ...], path: str) -> str:
    encoded = (_encode(item, f"{path}[{i}]") for i, item in enumerate(items))
    return "[" + ",".join(encoded) + "]"


def _encode_object(obj: dict[Any, Any], path: str) -> str:
    parts: list[str] = []
    for key in sorted(obj):
        if not isinstance(key, str):
            raise IRError(f"object key {key!r} at {path} is not a string")
        parts.append(json.dumps(key, ensure_ascii=False) + ":" + _encode(obj[key], f"{path}.{key}"))
    return "{" + ",".join(parts) + "}"
