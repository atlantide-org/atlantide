"""atlantide.ir: the Atlas IR — canonical, hashable, language-independent config."""

from atlantide.ir.artifact import Artifact, build_artifact, loads, verify_hash
from atlantide.ir.canonical import to_canonical_json
from atlantide.ir.hash import canonical_bytes, hash_ir
from atlantide.ir.lower import lower
from atlantide.ir.merkle import merkle_hashes
from atlantide.ir.model import IR_VERSION, IRGraph, IRNode

__all__ = [
    "IR_VERSION",
    "Artifact",
    "IRGraph",
    "IRNode",
    "build_artifact",
    "canonical_bytes",
    "hash_ir",
    "loads",
    "lower",
    "merkle_hashes",
    "to_canonical_json",
    "verify_hash",
]
