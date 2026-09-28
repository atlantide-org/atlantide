"""End to end: read-only commands never create the install keyfile.

``plan`` on a machine without the keyfile used to create a fresh random one,
whose salt matched none of the digests in state: every secret read as rotated,
and the wrong key left on disk later broke unsealing. Driven through the CLI
with a provider discovered like any plugin, whose resource has a sensitive input
(no shipped local resource has one), and secrets read from the environment so
that nothing but the run itself could create the key.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, ClassVar

import pytest

from atlantide.core import Context, Provider, Resource, computed, immutable, mutable
from atlantide.core.plugin import ProviderPlugin
from atlantide.providers import loader
from tests.support import Cli

cli = Cli()


class Vault(Resource):
    """A resource holding a secret in a sensitive input."""

    class Meta:
        provider = "kf2"

    vault_name: str = immutable(physical_name=True)
    token: str = mutable(default="", sensitive=True)
    serial: str = computed()


class _VaultProvider(Provider):
    name: ClassVar[str] = "kf2"
    version: ClassVar[str] = "1.0.0"

    async def create(self, ctx: Context, res: Resource) -> dict[str, Any]:
        return {"serial": res.node_id}

    async def read(self, ctx: Context, res: Resource) -> dict[str, Any] | None:
        return {"serial": res.node_id}

    async def update(self, ctx: Context, prior: dict[str, Any], res: Resource) -> dict[str, Any]:
        return {"serial": res.node_id}

    async def delete(self, ctx: Context, res: Resource) -> None:
        return None


_PLUGIN = ProviderPlugin(
    name="kf2",
    types={Vault.type_name(): Vault},
    factory=lambda settings: _VaultProvider(),
    module=__name__,
    summary="A test provider with a sensitive input.",
)


class _EntryPoint:
    name = "kf2"

    def load(self) -> ProviderPlugin:
        return _PLUGIN


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh project whose one resource takes a secret from ``$S0``."""
    installed = list(entry_points(group=loader.ENTRY_POINT_GROUP))
    monkeypatch.setattr(loader, "entry_points", lambda group: [*installed, _EntryPoint()])
    monkeypatch.setenv("S0", "hunter2")
    (tmp_path / "atlantide.toml").write_text(
        'config = "infra.py"\n\n[secrets]\nprovider = "env"\n\n[secrets.env]\nallow = ["S*"]\n'
    )
    (tmp_path / "infra.py").write_text(
        f"from atlantide.core import SecretRef\nfrom {__name__} import Vault\n\n"
        "Vault('v', vault_name='v', token=SecretRef('S0'))\n"
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_plan_apply_plan_then_a_lost_keyfile(project: Path) -> None:
    key = project / "atlantide.key"

    planned = cli.ok("plan")
    assert "create" in planned.output.lower()
    assert not key.exists()

    cli.ok("apply", "-y")
    assert key.exists()

    again = cli.ok("plan", "--detailed-exitcode")  # exit 0: no changes, no rotation
    assert "secrets_key" not in again.output

    key.unlink()
    failed = cli.fails("plan")
    assert "no keyfile at" in failed.output
    assert str(key) in failed.output.replace("\n", "")
    assert not key.exists()


def test_apply_dry_run_creates_no_key(project: Path) -> None:
    key = project / "atlantide.key"
    cli.ok("apply", "--dry-run")
    assert not key.exists()

    cli.ok("apply", "-y")
    key.unlink()
    failed = cli.fails("apply", "--dry-run")
    assert "no keyfile at" in failed.output
    assert not key.exists()


def test_validate_and_build_create_no_key(project: Path) -> None:
    cli.ok("validate")
    cli.ok("build", "-o", project / "out.atlas")
    assert not (project / "atlantide.key").exists()
