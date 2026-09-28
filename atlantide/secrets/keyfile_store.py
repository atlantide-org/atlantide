"""Default secrets backend: a local AES-256-GCM value-store (``name -> value``).

Needs no credentials. The store file holds
``STORE_V1_MAGIC || AES-GCM(JSON {name: value})``, authenticated with a store-only
AAD; the key lives in a sibling ``0600`` keyfile generated on first write. Values
are managed out-of-band via the CLI (``atlantide secret set/rm/list``) and never
written to config, the IR, or engine state.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar, override

from cryptography.exceptions import InvalidTag

from atlantide.core.check import FAIL, OK, Check
from atlantide.core.errors import SecretsError
from atlantide.secrets._aesgcm import decrypt, encrypt, load_key, load_or_create_key
from atlantide.secrets.base import SecretsProvider
from atlantide.util.fs import OWNER_ONLY_MODE, write_private

#: Header of the only accepted store format. A headerless file (bare
#: ``nonce || ciphertext``, no AAD) is refused.
STORE_V1_MAGIC = b"ATLSTORE1\n"

#: AAD binding the store file to this use of the key, so the store blob cannot
#: be replayed as a ``{"$sealed": ...}`` value under the same key (and vice versa).
_STORE_V1_AAD = b"atlantide/store/v1"


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

    @override
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
        with self._locked():
            values = self._load()
            values[name] = value
            self._save(values)

    def delete(self, name: str) -> bool:
        with self._locked():
            values = self._load()
            if name not in values:
                return False
            del values[name]
            self._save(values)
            return True

    def names(self) -> list[str]:
        return sorted(self._load())

    # -- preflight --------------------------------------------------------

    @override
    def check(self) -> Check:
        """Confirm the store opens with the key this install holds.

        Detects a store encrypted under a different key (an unshared or
        regenerated keyfile), which mid-apply resolution reports only as
        unreadable secrets. An absent store passes: a project may have no secrets.
        """
        if not self._store.exists():
            return self._check(OK, f"no store yet at {self._store}")
        try:
            values = self._load()
        except SecretsError as exc:
            return self._check(FAIL, self._why(exc))
        return self._check(OK, f"{len(values)} secret(s) in {self._store}")

    def _why(self, exc: SecretsError) -> str:
        """Describe a failed open in actionable terms.

        A decryption failure is identified by its ``__cause__``, not its message.
        AES-GCM authentication cannot distinguish a wrong key from damaged bytes,
        so the message names both.
        """
        if isinstance(exc.__cause__, InvalidTag | ValueError):
            return (
                f"cannot decrypt {self._store} with {self._key_path} — wrong or "
                f"regenerated keyfile, or a damaged store"
            )
        return str(exc)

    # -- storage ----------------------------------------------------------

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold an exclusive advisory lock around load-modify-save.

        Serializes concurrent CLI writes so none is lost. ``flock`` on a sibling
        lock file works on darwin and linux; closing the fd releases it.
        """
        self._store.parent.mkdir(parents=True, exist_ok=True)
        lock = self._store.with_suffix(self._store.suffix + ".lock")
        fd = os.open(str(lock), os.O_WRONLY | os.O_CREAT, OWNER_ONLY_MODE)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _load(self) -> dict[str, str]:
        """Decrypt the store; a missing file is an empty store."""
        if not self._store.exists():
            return {}
        blob = self._store.read_bytes()
        if not blob.startswith(STORE_V1_MAGIC):
            raise SecretsError(
                f"{self._store} is not an ATLSTORE1 secrets store (a legacy headerless "
                f"store is no longer supported) — move it aside and re-create each "
                f"secret with `atlantide secret set`"
            )
        raw = decrypt(self._load_key(create=False), blob[len(STORE_V1_MAGIC) :], aad=_STORE_V1_AAD)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise SecretsError("corrupt secrets store: expected a JSON object")
        return {str(k): str(v) for k, v in data.items()}

    def _save(self, values: dict[str, str]) -> None:
        plaintext = json.dumps(values, sort_keys=True).encode("utf-8")
        blob = STORE_V1_MAGIC + encrypt(self._load_key(), plaintext, aad=_STORE_V1_AAD)
        self._store.parent.mkdir(parents=True, exist_ok=True)
        # Owner-only fresh temp file, fsync, atomic replace, directory fsync: the
        # store is never world-readable or written through a planted symlink, and
        # neither a crash nor a power loss can leave this sole copy of every
        # secret truncated or empty. Callers hold ``_locked``.
        write_private(self._store, blob, overwrite=True)

    def _load_key(self, *, create: bool = True) -> bytes:
        """The key; ``create=False`` refuses to generate one (decrypt-only paths)."""
        if self._key is None:
            load = load_or_create_key if create else load_key
            self._key = load(self._key_path)
        return self._key
