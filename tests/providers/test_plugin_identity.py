"""One name per plugin: entry point, plugin, resource types and provider.

Resources are routed to a provider by name, and state rows are resolved to a
class through their type name. A plugin's factory must build the provider it is
named for, and its declared types must belong to it. Otherwise a plugin named
`zeta` could build provider `acme` (or `aws`) whenever the real one is absent,
or key a type map entry `acme.Gadget`, and handle resources that are not its
own.

The rule is checked twice: statically at discovery (the entry-point name, the
plugin's name and every declared type), and dynamically once the factory has
built the provider. Either refusal aborts every command that builds providers
through the same error path as a registration failure; `providers` and the
`state` commands keep working so the fault can be diagnosed.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, override

import pytest

from atlantide.core import Resource, immutable
from atlantide.core.plugin import ONE_NAME_RULE, ProviderPlugin
from atlantide.providers import loader
from atlantide.providers.loader import discover
from tests.providers.test_plugins import FakeEntryPoint, _entry_points, _gadget_config
from tests.support import Cli
from tests.support.fakeplugin import PLUGIN as ACME
from tests.support.fakeplugin import AcmeProvider, Gadget

cli = Cli()

_REPO = Path(__file__).resolve().parents[2]


class Squatter(Resource):
    """A class that belongs to `zeta`, keyed as another provider's type."""

    class Meta:
        provider = "zeta"

    thing: str = immutable(physical_name=True)


class Anonymous(Resource):
    """No `Meta.provider` at all: routed to no provider, owned by nobody."""

    thing: str = immutable(physical_name=True)


def _plan(tmp_path: Path, *extra: str) -> Any:
    return cli.fails("plan", _gadget_config(tmp_path), "--state", tmp_path / "s.db", *extra)


def _refused(name: str, detail: str) -> str:
    return f"provider plugin {name!r} could not be registered: {detail}"


# -- the static half: discovery ------------------------------------------------


def test_an_entry_point_named_differently_from_its_plugin_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Discovery orders and deduplicates by entry-point name; one that could
    differ from the plugin's name could sort ahead of, and take the name from,
    the plugin that owns it."""
    _entry_points(monkeypatch, FakeEntryPoint("acme-plugin", ACME))
    found = discover()

    assert not found.plugins
    [error] = found.errors
    assert (error.name, error.fatal) == ("acme-plugin", True)
    assert error.detail == (
        f"entry point 'acme-plugin' loads a plugin named 'acme' ({ONE_NAME_RULE})"
    )


def test_a_type_belonging_to_another_provider_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`zeta` declaring acme's `Gadget`: every `acme.Gadget` row in state would
    resolve to a plugin that is not acme and, with acme absent, be routed to
    whatever `zeta` builds."""
    zeta = replace(ACME, name="zeta", types={"acme.Gadget": Gadget})
    _entry_points(monkeypatch, FakeEntryPoint("zeta", zeta))
    found = discover()

    assert not found.plugins
    assert "acme.Gadget" not in found.types()
    [error] = found.errors
    assert error.fatal
    assert error.detail == (
        "type 'acme.Gadget' (tests.support.fakeplugin.Gadget) belongs to provider "
        f"'acme', not 'zeta' ({ONE_NAME_RULE})"
    )


def test_a_type_keyed_under_another_name_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A class under another provider's key still captures that key's rows:
    state resolves a row to a class through the type map, by the key."""
    zeta = replace(ACME, name="zeta", types={"acme.Gadget": Squatter}, factory=_zeta_factory)
    _entry_points(monkeypatch, FakeEntryPoint("zeta", zeta))
    [error] = discover().errors
    assert error.detail == (
        "type key 'acme.Gadget' names tests.providers.test_plugin_identity.Squatter, "
        f"whose type is 'zeta.Squatter' ({ONE_NAME_RULE})"
    )


def test_every_fault_in_one_plugin_is_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    """All faults are reported at once, not one per re-run."""
    zeta = replace(
        ACME,
        name="zeta",
        types={"acme.Gadget": Gadget, "Anonymous": Anonymous, "zeta.X": object},  # type: ignore[dict-item]
    )
    _entry_points(monkeypatch, FakeEntryPoint("zed", zeta))
    [error] = discover().errors
    assert error.detail.split("; ") == [
        "entry point 'zed' loads a plugin named 'zeta'",
        "type 'acme.Gadget' (tests.support.fakeplugin.Gadget) belongs to provider "
        "'acme', not 'zeta'",
        "type 'Anonymous' (tests.providers.test_plugin_identity.Anonymous) belongs to "
        "provider '', not 'zeta'",
        f"type 'zeta.X' (builtins.object) is not a Resource subclass ({ONE_NAME_RULE})",
    ]


def test_a_plugin_with_no_name_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _entry_points(monkeypatch, FakeEntryPoint("", replace(ACME, name="", types={})))
    [error] = discover().errors
    assert error.fatal
    assert error.detail.startswith("plugin name '' is not a non-empty string")


class _ExplodingTypes(Mapping[str, type[Resource]]):
    """A `types` mapping whose iteration runs plugin code that fails."""

    @override
    def __getitem__(self, key: str) -> type[Resource]:
        raise KeyError(key)

    @override
    def __iter__(self) -> Iterator[str]:
        raise RuntimeError("types unavailable")

    @override
    def __len__(self) -> int:
        return 1


def test_unreadable_types_are_a_load_failure_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plugin contributes nothing, like one whose import failed, and
    discovery of the others goes on."""
    broken = replace(ACME, name="zeta", types=_ExplodingTypes())
    _entry_points(monkeypatch, FakeEntryPoint("acme", ACME), FakeEntryPoint("zeta", broken))
    found = discover()
    assert [p.name for p in found.plugins] == ["acme"]
    [error] = found.errors
    assert (error.name, error.fatal) == ("zeta", False)
    assert "RuntimeError: types unavailable" in error.detail


@dataclass
class _InstalledEntryPoint:
    """An entry point with the metadata a real one carries: its target string
    and its distribution, which is what tells two same-named claimants apart."""

    name: str
    value: str
    dist: Any
    plugin: ProviderPlugin

    def load(self) -> ProviderPlugin:
        return self.plugin


@dataclass
class _Dist:
    name: str


def test_a_contested_name_names_both_distributions(monkeypatch: pytest.MonkeyPatch) -> None:
    _entry_points(
        monkeypatch,
        _InstalledEntryPoint("acme", "acme_atlantide:PLUGIN", _Dist("acme-atlantide"), ACME),  # type: ignore[arg-type]
        _InstalledEntryPoint("acme", "evil:PLUGIN", _Dist("evil"), ACME),  # type: ignore[arg-type]
    )
    [error] = discover().errors
    assert error.fatal
    assert error.detail == (
        "provider 'acme' is claimed by both entry point 'acme' (acme_atlantide:PLUGIN) "
        "from distribution 'acme-atlantide' and entry point 'acme' (evil:PLUGIN) from "
        "distribution 'evil'; two installed plugins cannot share a name — uninstall one"
    )


# -- the dynamic half: what the factory built ---------------------------------


def _zeta_factory(_settings: Mapping[str, Any]) -> AcmeProvider:
    return AcmeProvider()  # named `acme`, whatever plugin built it


def test_a_factory_building_an_absent_providers_name_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case the registry cannot catch: `acme` is not installed, so a
    provider named `acme` registers cleanly, and every `acme.*` resource in
    state would be routed to `zeta`'s code."""
    zeta = replace(ACME, name="zeta", types={}, factory=_zeta_factory)
    _entry_points(monkeypatch, FakeEntryPoint("zeta", zeta))
    result = _plan(tmp_path)
    expected = _refused("zeta", f"its factory built provider 'acme', not 'zeta' ({ONE_NAME_RULE})")
    assert f"error: {expected}" in result.output


def test_a_factory_building_a_nameless_provider_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Nameless(AcmeProvider):
        name = ""

    _entry_points(monkeypatch, FakeEntryPoint("acme", replace(ACME, factory=lambda _: Nameless())))
    result = _plan(tmp_path)
    assert (
        "provider plugin 'acme' could not be registered: its factory built a Nameless "
        "with no provider name, not provider 'acme'"
    ) in result.output


def test_provider_error_accepts_the_plugins_own_provider() -> None:
    assert ACME.provider_error(ACME.factory({})) is None
    assert ACME.identity_errors(entry_point="acme") == ()


# -- the CLI ------------------------------------------------------------------


def _type_squatter(monkeypatch: pytest.MonkeyPatch) -> None:
    zeta = replace(ACME, name="zeta", types={"acme.Gadget": Gadget}, factory=_zeta_factory)
    _entry_points(monkeypatch, FakeEntryPoint("zeta", zeta))


SQUATTED = _refused(
    "zeta",
    "type 'acme.Gadget' (tests.support.fakeplugin.Gadget) belongs to provider 'acme', "
    f"not 'zeta' ({ONE_NAME_RULE})",
)


def test_a_statically_refused_plugin_aborts_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused at discovery, so none of its code runs, but a warning would let a
    run proceed in which resources of that name may have been meant for it.
    Same path as a registration failure."""
    _type_squatter(monkeypatch)
    result = _plan(tmp_path)
    # Rich wraps at the terminal width; the message is longer than one line.
    assert f"error: {SQUATTED}" in " ".join(result.output.split())
    assert "warning" not in result.output


def test_an_identity_refusal_is_a_json_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _type_squatter(monkeypatch)
    payload = json.loads(_plan(tmp_path, "--json").stdout)
    assert payload["ok"] is False
    assert payload["error"]["kind"] == "RegistryError"
    assert payload["error"]["message"] == SQUATTED


def test_a_dynamic_refusal_is_a_json_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    zeta = replace(ACME, name="zeta", types={}, factory=_zeta_factory)
    _entry_points(monkeypatch, FakeEntryPoint("zeta", zeta))
    payload = json.loads(_plan(tmp_path, "--json").stdout)
    assert payload["error"]["kind"] == "RegistryError"
    assert payload["error"]["message"] == _refused(
        "zeta", f"its factory built provider 'acme', not 'zeta' ({ONE_NAME_RULE})"
    )


def test_every_refused_plugin_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _entry_points(
        monkeypatch,
        FakeEntryPoint("acme-plugin", ACME),
        FakeEntryPoint("zeta", replace(ACME, name="zeta", types={"acme.Gadget": Gadget})),
    )
    result = _plan(tmp_path)
    assert "error: provider plugin 'acme-plugin' could not be registered: entry point " in (
        result.output
    )
    assert "  and: provider plugin 'zeta' could not be registered: type 'acme.Gadget'" in (
        result.output
    )


def test_the_providers_command_shows_a_refused_plugin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Listed as refused rather than merely failed: it aborts every command that
    builds providers, not just the runs that would have used it."""
    _type_squatter(monkeypatch)
    result = cli.fails("providers")
    assert "refused zeta: type 'acme.Gadget'" in result.output

    payload = json.loads(cli.fails("providers", "--json").stdout)
    [error] = payload["errors"]
    assert error["name"] == "zeta"
    assert error["fatal"] is True
    assert "belongs to provider 'acme'" in error["detail"]


def test_a_refused_plugin_does_not_stop_the_diagnostic_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _type_squatter(monkeypatch)
    cli.ok("state", "list", "--state", tmp_path / "s.db")
    cli.ok("--no-plugins", "providers")


def test_no_plugins_is_the_way_around_a_refused_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _type_squatter(monkeypatch)
    cfg = tmp_path / "config.py"
    cfg.write_text("from atlantide.providers.random import Uuid\nUuid('u')\n")
    cli.ok("--no-plugins", "plan", cfg, "--state", tmp_path / "s.db")


# -- the built-ins ------------------------------------------------------------


def test_the_built_ins_are_one_name_throughout() -> None:
    """Each built-in's pyproject entry-point key, plugin name, declared types and
    built provider agree, on the entry-point path and on the fallback."""
    pyproject = tomllib.loads((_REPO / "pyproject.toml").read_text())
    advertised = pyproject["project"]["entry-points"]["atlantide.providers"]
    for key, target in advertised.items():
        module, _, attr = target.partition(":")
        plugin = getattr(__import__(module, fromlist=[attr]), attr)
        assert plugin.name == key
        assert plugin.identity_errors(entry_point=key) == ()
        assert plugin.provider_error(plugin.factory({})) is None

    assert discover().errors == ()
    assert loader._builtins_only().errors == ()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
