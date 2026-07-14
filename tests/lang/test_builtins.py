"""Atlas-lang derived builtins: determinism and in-language reachability."""

from __future__ import annotations

from typing import ClassVar

from atlantide.core import Resource, immutable, mutable
from atlantide.lang import evaluate_source
from atlantide.lang.builtins import (
    build_globals,
    from_json,
    hmac_sha256_hex,
    merge,
    slugify,
    to_json,
)


class Widget(Resource):
    """Test resource injected via extra_globals (no provider package yet)."""

    class Meta:
        provider: ClassVar[str] = "test"

    size: int = immutable()
    label: str = mutable(default="")


def _label(source: str, **kw: object) -> str:
    reg = evaluate_source(source, extra_globals={"Widget": Widget}, **kw).unwrap()  # type: ignore[arg-type]
    return reg.get("default:test.Widget:w").unwrap().label


# -- to_json / from_json -----------------------------------------------------


def test_to_json_is_canonical() -> None:
    # Keys sorted, no insignificant whitespace — byte-stable regardless of
    # insertion order.
    assert to_json({"b": 2, "a": [1, 2]}) == '{"a":[1,2],"b":2}'
    assert to_json({"b": 2, "a": 1}) == to_json({"a": 1, "b": 2})


def test_to_json_preserves_unicode() -> None:
    assert to_json({"name": "café"}) == '{"name":"café"}'


def test_from_json_round_trips() -> None:
    value = {"a": [1, 2], "b": {"c": True}}
    assert from_json(to_json(value)) == value


# -- merge -------------------------------------------------------------------


def test_merge_deep_and_later_wins() -> None:
    assert merge({"a": {"x": 1}}, {"a": {"y": 2}, "b": 3}) == {
        "a": {"x": 1, "y": 2},
        "b": 3,
    }
    assert merge({"a": 1}, {"a": 2}) == {"a": 2}


def test_merge_replaces_non_dicts() -> None:
    # A dict on the left overwritten by a scalar on the right is replaced whole.
    assert merge({"a": {"x": 1}}, {"a": 5}) == {"a": 5}


def test_merge_does_not_mutate_inputs() -> None:
    left = {"a": {"x": 1}}
    right = {"a": {"y": 2}}
    merge(left, right)
    assert left == {"a": {"x": 1}}
    assert right == {"a": {"y": 2}}


def test_merge_no_args_is_empty() -> None:
    assert merge() == {}


# -- slugify -----------------------------------------------------------------


def test_slugify_ascii_folds_and_lowercases() -> None:
    assert slugify("Café Menu!! v2") == "cafe-menu-v2"


def test_slugify_trims_and_collapses_separators() -> None:
    assert slugify("  Hello___World  ") == "hello-world"


def test_slugify_all_symbols_is_empty() -> None:
    assert slugify("!!!") == ""


# -- hmac_sha256_hex ---------------------------------------------------------


def test_hmac_is_deterministic() -> None:
    first = hmac_sha256_hex("key", "message")
    assert first == hmac_sha256_hex("key", "message")
    assert len(first) == 64


def test_hmac_depends_on_key() -> None:
    assert hmac_sha256_hex("key1", "m") != hmac_sha256_hex("key2", "m")


# -- in-language reachability ------------------------------------------------


def test_builtins_exposed_both_as_globals_and_config_api() -> None:
    scope = build_globals()
    for name in ("to_json", "from_json", "merge", "slugify", "hmac_sha256_hex"):
        assert name in scope
        assert hasattr(scope["atlantide"], name)


def test_slugify_reachable_in_config() -> None:
    assert _label("Widget('w', size=1, label=slugify('My App'))") == "my-app"


def test_merge_and_to_json_reachable_in_config() -> None:
    src = "Widget('w', size=1, label=to_json(merge({'a': 1}, {'b': 2})))"
    assert _label(src) == '{"a":1,"b":2}'


def test_hmac_reachable_via_config_api() -> None:
    src = "Widget('w', size=1, label=atlantide.hmac_sha256_hex('k', 'v')[:8])"
    assert _label(src) == hmac_sha256_hex("k", "v")[:8]
