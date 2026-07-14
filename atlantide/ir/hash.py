"""Content hashing of the Atlas IR.

``hash(IR)`` = SHA-256 of the canonical JSON. It is the plan identity (identical
config -> identical IR -> identical hash) and the artifact integrity check.
"""

from __future__ import annotations

import hashlib

from atlantide.ir.canonical import to_canonical_json
from atlantide.ir.model import IRGraph


def canonical_bytes(ir: IRGraph) -> bytes:
    return to_canonical_json(ir.to_canonical())


def hash_ir(ir: IRGraph) -> str:
    """Hex SHA-256 of the IR's canonical encoding (the plan identity)."""
    return hashlib.sha256(canonical_bytes(ir)).hexdigest()
