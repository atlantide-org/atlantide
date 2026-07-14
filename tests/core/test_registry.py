"""Provider registry and semver compatibility tests (Result-based API)."""

from __future__ import annotations

from atlantide.core import (
    ProviderRegistry,
    RegistryError,
    check_compatible,
    is_successful,
    parse_semver,
)
from tests.support import FakeProvider


def _dummy() -> FakeProvider:
    return FakeProvider(name="dummy", version="1.4.2")


def test_parse_semver_success() -> None:
    assert parse_semver("1.4.2").unwrap() == (1, 4, 2)


def test_parse_semver_failure() -> None:
    for bad in ("1.4", "v1.4.2"):
        result = parse_semver(bad)
        assert not is_successful(result)
        assert isinstance(result.failure(), RegistryError)


def test_check_compatible_same_major() -> None:
    assert is_successful(check_compatible("1.4.2", "1.9.0"))  # minor/patch drift ok
    assert is_successful(check_compatible("1.9.0", "1.4.2"))


def test_check_compatible_major_mismatch() -> None:
    result = check_compatible("1.4.2", "2.0.0")
    assert not is_successful(result)
    assert "incompatible" in str(result.failure())


def test_register_get_and_duplicates() -> None:
    reg = ProviderRegistry()
    provider = _dummy()
    assert is_successful(reg.register(provider))
    assert reg.get("dummy").unwrap() is provider
    assert "dummy" in reg

    dup = reg.register(_dummy())
    assert not is_successful(dup)
    assert "duplicate provider" in str(dup.failure())

    missing = reg.get("nope")
    assert not is_successful(missing)
    assert "unknown provider" in str(missing.failure())


def test_registry_semver_gate() -> None:
    reg = ProviderRegistry()
    reg.register(_dummy())
    assert reg.check_compatible("dummy", "1.0.0").unwrap() is reg.get("dummy").unwrap()

    bad = reg.check_compatible("dummy", "2.1.0")
    assert not is_successful(bad)
    assert "incompatible" in str(bad.failure())


def test_bad_version_rejected_at_registration() -> None:
    result = ProviderRegistry().register(FakeProvider(name="bad", version="not-semver"))
    assert not is_successful(result)
    assert "invalid semver" in str(result.failure())


def test_missing_name_rejected() -> None:
    result = ProviderRegistry().register(FakeProvider(name=""))
    assert not is_successful(result)
    assert "declares no name" in str(result.failure())
