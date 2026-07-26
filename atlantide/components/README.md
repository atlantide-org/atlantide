# atlantide.components

Published components: reusable L2 constructs shared in a git repo, pinned into a
project, and imported from config as `atlantide.components.<alias>`.

| Module | Purpose |
| --- | --- |
| `__init__.py` | `mount`, which makes vendored trees importable under this package's `__path__`, and verifies each against the lock before doing so. |
| `source.py` | `ComponentSource` — the declared request: repo, ref, subdir, from `[components.<alias>]` in `atlantide.toml`. |
| `fetch.py` | `fetch` / `vendor` / `verify`: clone at a ref, copy the package into `.atlantis/components/<alias>`, and hash the tree. |
| `lock.py` | `atlantide.lock` — the resolved pins: exact commit plus a content hash of the vendored tree. |

Fetching is a separate, pinned step (the `terraform init` model) rather than a
live import, because Atlas-lang config cannot do network IO and must lower to
byte-stable IR. Mounting the result under this package's namespace means the
sandbox's import rules apply unchanged.

A published component is third-party Python that runs unsandboxed, like a
provider. Integrity rests on pinning: `mount` re-hashes every vendored tree
against the lock, so `plan`/`apply`/`build` refuse a tampered or stale
component rather than importing it.
