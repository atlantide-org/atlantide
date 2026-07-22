"""Default secrets backend: a local AES-256-GCM value-store (``name -> value``).

Creds-free. The store file holds ``AES-GCM(JSON {name: value})``; the key lives
in a sibling ``0600`` keyfile, auto-generated on first write. Managed out-of-band
via the CLI (``atlantide secret set/rm/list``) — values are never written to
config, the IR, or engine state.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import ClassVar

from cryptography.exceptions import InvalidTag

from atlantide.core.check import FAIL, OK, Check
from atlantide.core.errors import SecretsError
from atlantide.secrets._aesgcm import decrypt, encrypt, load_or_create_key
from atlantide.secrets.backend import SecretsProvider


class KeyfileValueStore(SecretsProvider):
    """An encrypted local ``name -> value`` store, resolved at apply time."""

    name: ClassVar[str] = "keyfile"

    def __init__(
        self, store_path: str | os.PathLike[str], key_path: str | os.PathLike[str]
    ) -> None:
        self._store = Path(store_path)
        self._key_path = Path(key_path)
        self._key: bytes | None = None

    # -- resolution -------------------------------------------------------

    def resolve(self, name: str) -> str:
        values = self._load()
        if name not in values:
            raise SecretsError(
                f"secret {name!r} not found in the keyfile store — "
                f"run `atlantide secret set {name} ...`"
            )
        return values[name]

    # -- management (CLI) -------------------------------------------------

    def set(self, name: str, value: str) -> None:
        values = self._load()
        values[name] = value
        self._save(values)

    def delete(self, name: str) -> bool:
        values = self._load()
        if name not in values:
            return False
        del values[name]
        self._save(values)
        return True

    def names(self) -> list[str]:
        return sorted(self._load())

    # -- preflight --------------------------------------------------------

    def check(self) -> Check:
        """Confirm the store opens with the key this install holds.

        The failure worth catching is a store encrypted under a *different* key —
        a keyfile not shared with the rest of the team, or one regenerated after
        being lost. Resolution only happens mid-apply, and the symptom there
        (every secret unreadable) does not name its cause.

        An absent store is not a failure: a project may simply have no secrets.
        """
        if not self._store.exists():
            return Check(f"secrets: {self.name}", OK, f"no store yet at {self._store}")
        try:
            values = self._load()
        except SecretsError as exc:
            return Check(f"secrets: {self.name}", FAIL, self._why(exc))
        return Check(
            f"secrets: {self.name}", OK, f"{len(values)} secret(s) in {self._store}"
        )

    def _why(self, exc: SecretsError) -> str:
        """Explain a failed open, in terms of what the operator can act on.

        A decryption failure is identified by what it wraps, not by its wording.
        AES-GCM authentication cannot say whether the key is wrong or the bytes
        are damaged — the two are the same failure — so the message names both
        instead of guessing.
        """
        if isinstance(exc.__cause__, InvalidTag | ValueError):
            return (
                f"cannot decrypt {self._store} with {self._key_path} — wrong or "
                f"regenerated keyfile, or a damaged store"
            )
        return str(exc)

    # -- storage ----------------------------------------------------------

    def _load(self) -> dict[str, str]:
        if not self._store.exists():
            return {}
        raw = decrypt(self._load_key(), self._store.read_bytes())
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise SecretsError("corrupt secrets store: expected a JSON object")
        return {str(k): str(v) for k, v in data.items()}

    def _save(self, values: dict[str, str]) -> None:
        blob = encrypt(self._load_key(), json.dumps(values, sort_keys=True).encode("utf-8"))
        self._store.parent.mkdir(parents=True, exist_ok=True)
        # Write to a 0600 temp file and replace atomically, so the store is never
        # world-readable and a crash cannot leave it half-written.
        tmp = self._store.with_suffix(self._store.suffix + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, blob)
        finally:
            os.close(fd)
        os.replace(tmp, self._store)

    def _load_key(self) -> bytes:
        if self._key is None:
            self._key = load_or_create_key(self._key_path)
        return self._key
