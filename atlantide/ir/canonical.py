"""Canonical JSON encoding (RFC 8785 JCS-style).

The same logical value must always encode to the *same bytes* so ``hash(IR)`` is
a stable plan identity across runs and machines. Enforces: sorted object keys,
compact separators, UTF-8, minimal string escaping, and no NaN/Infinity.

Object keys are sorted by Unicode code point; floats use Python's shortest
``repr``.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from pydantic import BaseModel

from atlantide.core.errors import IRError

# Root path shown when an encode error happens at the top-level value.
_ROOT = "<root>"


def to_canonical_json(value: Any) -> bytes:
    """Encode ``value`` to canonical UTF-8 JSON bytes."""
    text = _encode(value, _ROOT)
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as exc:
        # A lone surrogate survives `json.dumps(ensure_ascii=False)` but has no
        # UTF-8 form. Located only on this error path, so the common case pays
        # nothing for it.
        path = _unencodable_path(value, _ROOT) or _ROOT
        raise IRError(f"string at {path} contains a lone surrogate and is not encodable") from exc


def _encode(value: Any, path: str) -> str:
    """Encode one value. ``path`` locates ``value`` in the tree for error messages."""
    if value is None:
        return "null"
    # bool is a subclass of int, so it must be checked before the int branch.
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
    if isinstance(value, BaseModel):
        # A structured field (e.g. a security-group rule) encodes as its mapping, so
        # it hashes like an untyped dict. `mode="json"` normalises enums and dates.
        return _encode_object(value.model_dump(mode="json"), path)
    raise IRError(f"value of type {type(value).__name__} at {path} is not JSON-encodable")


def _encode_float(value: float, path: str) -> str:
    if math.isnan(value) or math.isinf(value):
        raise IRError(f"non-finite float {value!r} at {path} is not encodable")
    return repr(value)


def _encode_array(items: list[Any] | tuple[Any, ...], path: str) -> str:
    encoded = (_encode(item, f"{path}[{i}]") for i, item in enumerate(items))
    return "[" + ",".join(encoded) + "]"


def _encode_object(obj: dict[Any, Any], path: str) -> str:
    # Checked before sorting: mixed-type keys would make `sorted` raise TypeError.
    for key in obj:
        if not isinstance(key, str):
            raise IRError(f"object key {key!r} at {path} is not a string")
    parts = [
        json.dumps(key, ensure_ascii=False) + ":" + _encode(obj[key], f"{path}.{key}")
        for key in sorted(obj)
    ]
    return "{" + ",".join(parts) + "}"


def _unencodable_path(value: Any, path: str) -> str | None:
    """Path of the first string (value or key) under ``value`` with no UTF-8 form.

    Walks the same shapes :func:`_encode` accepts; ``value`` has already encoded,
    so every key is a string.
    """
    if isinstance(value, str):
        return None if _utf8_encodable(value) else path
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        for key in sorted(value):
            if not _utf8_encodable(key):
                return f"{path}.{key!r}"
            found = _unencodable_path(value[key], f"{path}.{key}")
            if found is not None:
                return found
    elif isinstance(value, list | tuple):
        for i, item in enumerate(value):
            found = _unencodable_path(item, f"{path}[{i}]")
            if found is not None:
                return found
    return None


def _utf8_encodable(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def canonical_sha256(value: Any) -> str:
    """Hex SHA-256 of ``value``'s canonical encoding.

    Shared by the IR identity (:func:`~atlantide.ir.hash.hash_ir`) and the
    per-node Merkle digests so both hash the same canonical bytes.
    """
    return hashlib.sha256(to_canonical_json(value)).hexdigest()
