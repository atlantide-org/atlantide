"""A keyfile others can read is refused.

Creation is 0600, but a key restored from a backup or copied under a lax umask
arrives readable by everyone — and it decrypts the local secret store.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atlantide.core.errors import SecretsError
from atlantide.secrets._aesgcm import load_or_create_key


def test_a_world_readable_keyfile_is_refused(tmp_path: Path) -> None:
    """Creation is 0600, but a key restored from a backup or copied under a lax
    umask can arrive readable by everyone."""
    path = tmp_path / "key"
    load_or_create_key(path)  # created 0600
    path.chmod(0o644)

    with pytest.raises(SecretsError, match="readable by"):
        load_or_create_key(path)


def test_a_correctly_scoped_keyfile_still_loads(tmp_path: Path) -> None:
    path = tmp_path / "key"
    created = load_or_create_key(path)
    assert load_or_create_key(path) == created


def test_losing_the_creation_race_loads_the_winners_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If another process creates the keyfile mid-create, ours must load theirs
    rather than crash with FileExistsError or overwrite the winning key."""
    import os

    path = tmp_path / "key"
    winner = os.urandom(32)
    real_link = os.link

    def racing_link(src: str, dst: str, **kwargs: object) -> None:
        path.write_bytes(winner)  # the "other process" wins first
        path.chmod(0o600)
        real_link(src, dst, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "link", racing_link)
    assert load_or_create_key(path) == winner
