# atlantide.ir

The Atlas IR: the canonical, hashable, language-independent form of a config.
Everything downstream of evaluation — diff, graph, artifacts — reads IR, never
the Python objects that produced it.

Determinism lives here. Two runs of the same config lower to byte-identical IR
and the same content hash, which is what makes the Merkle skip and the `.atlas`
artifact trustworthy.

| Module | Purpose |
| --- | --- |
| `model.py` | `IRNode` / `IRGraph`, plus the hashed (`to_canonical`) and stored (`to_stored`) shapes. |
| `lower.py` | Lowers an evaluated `ResourceRegistry` into IR. |
| `canonical.py` | Canonical JSON encoding (RFC 8785 JCS-style): sorted keys, typed encoding, no insignificant whitespace. |
| `hash.py` | Content hash of a whole IR graph. |
| `merkle.py` | Per-node `input_hash` folding in type, properties, and dependency hashes — the basis of NOOP-skipping. |
| `artifact.py` | Deployable `.atlas` artifacts: IR plus provider pins, policy bindings, and outputs, content-hashed. |
