"""Marker predicates: `is_ref_marker` is strict so parsers never see bad shapes.

A dict that merely carries a ``$ref`` key is data unless its value can actually
split into ``node_id#attr`` — otherwise ``ref_from_marker`` raises a raw
``ValueError`` from deep inside the diff or a state migration.
"""

from __future__ import annotations

from atlantide.core.markers import (
    is_ref_marker,
    is_ref_or_marker,
    ref_from_marker,
    remap_refs,
)


def test_a_hashless_ref_value_is_not_a_marker() -> None:
    assert not is_ref_marker({"$ref": "no-hash"})
    assert not is_ref_or_marker({"$ref": "no-hash"})


def test_a_well_formed_marker_still_parses() -> None:
    marker = {"$ref": "default:test.Box:a#out"}
    assert is_ref_marker(marker)
    ref = ref_from_marker(marker)
    assert ref.node_id == "default:test.Box:a"
    assert ref.attr == "out"


def test_remap_passes_a_hashless_ref_key_through_as_data() -> None:
    """The rename migration walks whole state trees; a user value that happens to
    carry the key must ride through untouched rather than crash the migration."""
    value = {"outer": {"$ref": "no-hash"}, "real": {"$ref": "old#attr"}}
    remapped = remap_refs(value, {"old": "new", "no-hash": "clobbered"})
    assert remapped == {"outer": {"$ref": "no-hash"}, "real": {"$ref": "new#attr"}}
