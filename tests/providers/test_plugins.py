"""Provider discovery: third-party packages become usable providers.

A provider is an ordinary package implementing the ABC. Registering its types is
not enough: the import allow-list must also let config name the plugin's module.

Everything here drives the real entry-point path with a stub `entry_points`,
because a test that bypassed discovery and registered the provider by hand would
pass whether or not discovery works.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, ClassVar

import pytest

from atlantide.core.plugin import API_VERSION
from atlantide.lang import LanguageSurface, evaluate_source, validate_source
from atlantide.providers import loader
from atlantide.providers.loader import discover
from tests.support import Cli
from tests.support.fakeplugin import PLUGIN as ACME
from tests.support.fakeplugin import AcmeProvider

cli = Cli()


@dataclass
class FakeEntryPoint:
    """Stands in for an installed distribution's entry point."""

    name: str
    value: Any
    fails: Exception | None = None

    def load(self) -> Any:
        if self.fails is not None:
            raise self.fails
        return self.value


def _entry_points(monkeypatch: pytest.MonkeyPatch, *entries: FakeEntryPoint) -> None:
    monkeypatch.setattr(loader, "entry_points", lambda group: list(entries))


# -- discovery ----------------------------------------------------------------


def test_a_third_party_plugin_is_discovered(monkeypatch: pytest.MonkeyPatch) -> None:
    _entry_points(monkeypatch, FakeEntryPoint("acme", ACME))
    found = discover()
    assert [p.name for p in found.plugins] == ["acme"]
    assert "acme.Gadget" in found.types()


def test_the_built_in_providers_come_through_the_same_door() -> None:
    """The built-ins use the same discovery path as third-party plugins, so that
    path is exercised and cannot drift from a separate one."""
    found = discover()
    assert {"aws", "local", "random"} <= {p.name for p in found.plugins}


def test_a_broken_plugin_is_reported_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """`atlantide --version` and `atlantide state unlock` are the commands someone
    runs while fixing a broken install."""
    _entry_points(
        monkeypatch,
        FakeEntryPoint("acme", ACME),
        FakeEntryPoint("broken", None, fails=ImportError("no module named boom")),
    )
    found = discover()

    assert [p.name for p in found.plugins] == ["acme"], "the good one still loaded"
    assert [e.name for e in found.errors] == ["broken"]
    assert "no module named boom" in found.errors[0].detail


def test_something_that_is_not_a_plugin_is_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _entry_points(monkeypatch, FakeEntryPoint("wrong", object()))
    found = discover()
    assert not found.plugins
    assert "not a ProviderPlugin" in found.errors[0].detail


def test_a_plugin_speaking_another_api_version_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Half-loading it would surface a version mismatch as an attribute error deep
    in a run."""
    _entry_points(monkeypatch, FakeEntryPoint("future", replace(ACME, api_version=API_VERSION + 1)))
    found = discover()
    assert not found.plugins
    assert "plugin api" in found.errors[0].detail


def test_two_plugins_claiming_one_name_are_a_fatal_contest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two installed plugins named `acme` necessarily share the entry-point name
    too (the one-name rule), so nothing orders them but the metadata's
    enumeration. Neither is picked silently: the first is still listed, so
    `atlantide providers` shows both sides, and the contest is fatal."""
    other = replace(ACME, summary="a different acme")
    _entry_points(
        monkeypatch,
        FakeEntryPoint("acme", ACME),
        FakeEntryPoint("acme", other),
    )
    found = discover()

    assert [p.summary for p in found.plugins] == [ACME.summary]
    assert [(e.name, e.fatal) for e in found.errors] == [("acme", True)]
    assert "provider 'acme' is claimed by both entry point 'acme' and entry point 'acme'" in (
        found.errors[0].detail
    )


def test_discovery_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """For reproducing a build without whatever happens to be installed, and for
    bisecting a plugin that has broken a run."""
    _entry_points(monkeypatch, FakeEntryPoint("acme", ACME))
    found = discover(enabled=False)
    assert "acme" not in {p.name for p in found.plugins}
    assert {"aws", "local", "random"} == {p.name for p in found.plugins}


def test_unreadable_metadata_still_yields_the_built_ins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PyInstaller binary or zipapp has no metadata to read; the built-ins must
    still load."""
    _entry_points(monkeypatch)  # nothing advertised at all
    found = discover()
    assert {"aws", "local", "random"} == {p.name for p in found.plugins}


# -- the import surface -------------------------------------------------------


def test_a_plugin_module_is_not_importable_by_default() -> None:
    """Registering the types is not enough: config cannot import the module they
    live in until it is discovered."""
    source = "from tests.support.fakeplugin import Gadget\n"
    assert validate_source(source).failure() is not None


def test_a_plugin_module_becomes_importable_once_discovered() -> None:
    surface = LanguageSurface(extra=frozenset({"tests.support.fakeplugin"}))
    source = "from tests.support.fakeplugin import Gadget\nGadget('g', gadget_name='x')\n"

    registry = evaluate_source(source, surface=surface).unwrap()

    assert "default:acme.Gadget:g" in {r.node_id for r in registry.all()}


def test_a_plugins_internal_modules_stay_off_limits() -> None:
    """A plugin's `provider`/`handlers` submodules hold its network and
    filesystem calls, as the built-ins' do. Widening the surface must not widen
    that."""
    surface = LanguageSurface(extra=frozenset({"acme_plugin"}))
    for module in ("acme_plugin.provider", "acme_plugin.handlers.thing"):
        result = validate_source(f"from {module} import X\n", surface=surface)
        assert result.failure() is not None, module


def test_widening_the_surface_does_not_open_the_rest_of_atlantide() -> None:
    surface = LanguageSurface(extra=frozenset({"tests.support.fakeplugin"}))
    source = "from atlantide.state import MemoryStateBackend\n"
    assert validate_source(source, surface=surface).failure()


# -- settings -----------------------------------------------------------------


def test_a_plugin_factory_receives_its_settings_table() -> None:
    """A third party may accept settings this codebase knows nothing about, so the
    factory takes a raw mapping."""
    provider = ACME.factory({"marker": "configured"})
    assert provider.marker == "configured"  # type: ignore[attr-defined]


def test_a_factory_with_no_settings_still_builds() -> None:
    assert ACME.factory({}).marker == "default"  # type: ignore[attr-defined]


# -- the CLI ------------------------------------------------------------------


def test_the_providers_command_lists_what_is_installed() -> None:
    result = cli.run("providers")
    for name in ("aws", "local", "random"):
        assert name in result.output


def test_the_providers_command_surfaces_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plugin that does not load is otherwise invisible: config cannot find its
    types, which reads as a typo in the config."""
    _entry_points(
        monkeypatch,
        FakeEntryPoint("acme", ACME),
        FakeEntryPoint("broken", None, fails=ImportError("boom")),
    )
    result = cli.run("providers")
    assert result.exit_code == 1
    assert "broken" in result.output
    assert "boom" in result.output


def test_no_plugins_limits_the_command_to_the_built_ins(monkeypatch: pytest.MonkeyPatch) -> None:
    _entry_points(monkeypatch, FakeEntryPoint("acme", ACME))
    result = cli.run("--no-plugins", "providers")
    assert "acme" not in result.output


def test_a_config_can_use_a_discovered_third_party_resource(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end, through the real CLI: discovery, registration, the import
    surface and the type registry all have to line up for this to plan."""
    _entry_points(monkeypatch, FakeEntryPoint("acme", ACME))
    cfg = tmp_path / "config.py"
    cfg.write_text(
        "from tests.support.fakeplugin import Gadget\nGadget('widget', gadget_name='w1', size=3)\n"
    )

    result = cli.run("plan", cfg, "--state", tmp_path / "s.db")
    assert "acme.Gadget:widget" in result.output


def test_a_third_party_resource_applies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _entry_points(monkeypatch, FakeEntryPoint("acme", ACME))
    cfg = tmp_path / "config.py"
    cfg.write_text(
        "from atlantide.core import output\n"
        "from tests.support.fakeplugin import Gadget\n"
        "g = Gadget('widget', gadget_name='w1')\n"
        "output('serial', g.serial)\n"
    )

    result = cli.run("apply", cfg, "--state", tmp_path / "s.db", "-y")
    assert "default:acme.Gadget:widget" in result.output


# -- registration -------------------------------------------------------------
#
# A plugin can load cleanly and still be refused by the registry: a version that
# is not semver, or a provider name another plugin already holds. Its types are
# in the type map and its module is on the import surface either way, so an
# ignored refusal would surface as "unknown provider" far from its cause. The
# refusal aborts the command instead, naming the plugin.


class _BadVersion(AcmeProvider):
    version: ClassVar[str] = "banana"


class _NextMajor(AcmeProvider):
    version: ClassVar[str] = "2.0.0"


def _gadget_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.py"
    cfg.write_text(
        "from tests.support.fakeplugin import Gadget\nGadget('widget', gadget_name='w1')\n"
    )
    return cfg


def _bad_semver(monkeypatch: pytest.MonkeyPatch) -> None:
    _entry_points(
        monkeypatch, FakeEntryPoint("acme", replace(ACME, factory=lambda _: _BadVersion()))
    )


BAD_SEMVER = (
    "provider plugin 'acme' could not be registered: "
    "invalid semver 'banana': expected MAJOR.MINOR.PATCH"
)


def test_a_plugin_with_an_invalid_version_aborts_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bad_semver(monkeypatch)
    result = cli.fails("plan", _gadget_config(tmp_path), "--state", tmp_path / "s.db")
    assert f"error: {BAD_SEMVER}" in result.output
    assert "unknown provider" not in result.output


def test_a_plugin_taking_another_providers_name_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two plugins with distinct names whose factories build the same provider.
    The impostor sorts first, so a registry-only check would have let it take
    `acme` and blamed the real plugin for the duplicate; the name check refuses
    the impostor itself, before it registers anything."""
    impostor = replace(ACME, name="a-impostor", types={}, summary="")
    _entry_points(
        monkeypatch,
        FakeEntryPoint("acme", ACME),
        FakeEntryPoint("a-impostor", impostor),
    )
    result = cli.fails("plan", _gadget_config(tmp_path), "--state", tmp_path / "s.db")
    assert (
        "error: provider plugin 'a-impostor' could not be registered: its factory built "
        "provider 'acme', not 'a-impostor'"
    ) in result.output


def test_a_plugin_building_a_provider_under_another_name_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The name is checked before the registry sees the provider, so the
    misnaming is the reported fault even when the version is bad too."""
    misnamed = replace(ACME, name="zeta", types={}, factory=lambda _: _BadVersion())
    _entry_points(monkeypatch, FakeEntryPoint("zeta", misnamed))
    result = cli.fails("plan", _gadget_config(tmp_path), "--state", tmp_path / "s.db")
    assert (
        "provider plugin 'zeta' could not be registered: its factory built provider "
        "'acme', not 'zeta'"
    ) in result.output
    assert "banana" not in result.output


def test_a_registration_failure_is_a_json_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bad_semver(monkeypatch)
    result = cli.fails("plan", _gadget_config(tmp_path), "--state", tmp_path / "s.db", "--json")
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["error"]["kind"] == "RegistryError"
    assert payload["error"]["message"] == BAD_SEMVER


def test_a_registration_failure_does_not_stop_the_diagnostic_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Commands that build no providers — listing plugins, reading state — are
    the ones someone runs while fixing the plugin."""
    _bad_semver(monkeypatch)
    cli.ok("providers")
    cli.ok("state", "list", "--state", tmp_path / "s.db")


def test_an_incompatible_pin_is_refused_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An artifact built against acme 1.0.0 must not deploy against a 2.x that
    registered fine; the refusal names both versions."""
    _entry_points(monkeypatch, FakeEntryPoint("acme", ACME))
    art = tmp_path / "app.atlas"
    cli.ok("build", _gadget_config(tmp_path), "-o", art)

    _entry_points(
        monkeypatch, FakeEntryPoint("acme", replace(ACME, factory=lambda _: _NextMajor()))
    )
    expected = (
        "provider version incompatible: plan pinned 1.0.0, registered 2.0.0 "
        "(needs 1.x at or above 1.0.0)"
    )
    assert f"error: {expected}" in cli.fails("verify", art).output
    deployed = cli.fails("deploy", art, "--state", tmp_path / "s.db", "-y")
    assert f"error: {expected}" in deployed.output


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
