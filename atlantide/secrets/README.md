# atlantide.secrets

Secrets are referenced by name and resolved to values only at apply. A resource
field holds a `SecretRef`; config, IR, and state carry the handle alone.

Depends only on `atlantide.core` (enforced by an import contract).

| Module | Purpose |
| --- | --- |
| `registry.py` | `SecretsRegistry`: name → configured provider, with a default. Also seals/unseals values and checks rotation digests. |
| `backend.py` | The `SecretsProvider` interface. |
| `keyfile_store.py` | Default backend: a local AES-256-GCM value store. |
| `env.py` | Development backend: resolves straight from the environment. |
| `ssm.py` | Remote backend: AWS SSM Parameter Store. |
| `factory.py` | `SecretsConfig` → a configured registry. |
| `material.py` | Per-install key material, lazily loaded from the keyfile. |
| `digest.py` | Secret-reference markers and the salted rotation digest. |
| `_aesgcm.py` | AES-256-GCM primitives and local key management. |

Plaintext is resolved in memory and never persisted. State keeps a salted digest
of each resolved value so a rotation is detectable, and *seals* sensitive
computed outputs at rest. Renderers redact sensitive fields at the output
boundary.
