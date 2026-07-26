"""Provider discovery: third-party packages become usable providers.

The claim being tested is the one `providers/README.md` used to make and the code
did not honour — that a provider is an ordinary package implementing the ABC.
Registration was never the blocker; the import allow-list was, so a plugin could
register its types and config still could not name them.

Everything here drives the real entry-point path with a stub `entry_points`,
because a test that reached past discovery and registered the provider by hand
would pass whether or not any of this worked.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from atlantide.core.plugin import API_VERSION
from atlantide.lang import LanguageSurface, evaluate_source, validate_source
from atlantide.providers import loader
from atlantide.providers.loader import discover
from tests.support import Cli
from tests.support.fakeplugin import PLUGIN as ACME

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
    """A door only the shipped providers can walk through is a door nobody tests
    — and it drifts the moment the two paths diverge."""
    found = discover()
    assert {"aws", "local", "random"} <= {p.name for p in found.plugins}


def test_a_broken_plugin_is_reported_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """`atlantide --version` and `atlantide state unlock` are exactly the commands
    someone runs while trying to fix a broken install."""
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
    """Half-loading it would surface as an attribute error somewhere deep in a
    run, which is a bad way to learn about a version mismatch."""
    from dataclasses import replace

    _entry_points(monkeypatch, FakeEntryPoint("future", replace(ACME, api_version=API_VERSION + 1)))
    found = discover()
    assert not found.plugins
    assert "plugin api" in found.errors[0].detail


def test_two_plugins_claiming_one_name_resolve_deterministically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First by entry-point name wins, and the loser is reported. Whichever rule
    is picked, it has to be the same on two runs of the same machine."""
    from dataclasses import replace

    other = replace(ACME, summary="a different acme")
    _entry_points(
        monkeypatch,
        FakeEntryPoint("zzz-acme", other),
        FakeEntryPoint("aaa-acme", ACME),
    )
    found = discover()

    assert len(found.plugins) == 1
    assert found.plugins[0].summary == ACME.summary, "sorted by entry-point name"
    assert "already provided by" in found.errors[0].detail


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
    """A PyInstaller binary or zipapp has no metadata to read. A tool with *no*
    providers is useless in a way that a tool without third-party ones is not."""
    _entry_points(monkeypatch)  # nothing advertised at all
    found = discover()
    assert {"aws", "local", "random"} == {p.name for p in found.plugins}


# -- the import surface -------------------------------------------------------


def test_a_plugin_module_is_not_importable_by_default() -> None:
    """The actual blocker. Registering the types was never enough: config could
    not name the module they live in."""
    source = "from tests.support.fakeplugin import Gadget\n"
    assert validate_source(source).failure() is not None


def test_a_plugin_module_becomes_importable_once_discovered() -> None:
    surface = LanguageSurface(extra=frozenset({"tests.support.fakeplugin"}))
    source = "from tests.support.fakeplugin import Gadget\nGadget('g', gadget_name='x')\n"

    registry = evaluate_source(source, surface=surface).unwrap()

    assert "default:acme.Gadget:g" in {r.node_id for r in registry.all()}


def test_a_plugins_internal_modules_stay_off_limits() -> None:
    """A plugin's `provider`/`handlers` submodules hold its network and
    filesystem calls, exactly as the built-ins' do. Widening the surface must not
    widen that."""
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
    """A third party may accept settings this codebase knows nothing about —
    which is the thing being fixed, so the factory takes a raw mapping."""
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
    """A plugin that does not load is otherwise invisible: config simply cannot
    find its types, which reads as a typo in the config."""
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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
