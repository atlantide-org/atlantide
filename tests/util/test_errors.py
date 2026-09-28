"""`atlantide.util.errors`: failures riding along on another exception.

The attribute name is fixed (`tests/cli/test_interrupt.py` sets it by hand and
the CLI renderers read it), so the helpers must interoperate with the raw
attribute in both directions.
"""

from __future__ import annotations

from atlantide.cli.views.output import error_json
from atlantide.core.errors import AtlantideError, ProviderError
from atlantide.util.errors import ALSO_FAILED_ATTR, also_failed, attach_also_failed


def test_attribute_name_is_fixed() -> None:
    assert ALSO_FAILED_ATTR == "_also_failed"


def test_nothing_attached_reads_as_empty() -> None:
    assert also_failed(RuntimeError("x")) == []


def test_attach_then_read() -> None:
    primary = RuntimeError("primary")
    extras = [ValueError("a"), KeyError("b")]
    attach_also_failed(primary, extras)
    assert also_failed(primary) == extras


def test_attach_accepts_any_iterable_and_stores_a_list() -> None:
    primary = RuntimeError("primary")
    extra = ValueError("a")
    attach_also_failed(primary, iter([extra]))
    assert getattr(primary, ALSO_FAILED_ATTR) == [extra]


def test_attach_replaces_an_earlier_list() -> None:
    primary = RuntimeError("primary")
    attach_also_failed(primary, [ValueError("old")])
    new = ValueError("new")
    attach_also_failed(primary, [new])
    assert also_failed(primary) == [new]


def test_read_returns_a_copy() -> None:
    primary = RuntimeError("primary")
    attach_also_failed(primary, [ValueError("a")])
    also_failed(primary).clear()
    assert len(also_failed(primary)) == 1


def test_reads_an_attribute_set_by_hand() -> None:
    error = AtlantideError("primary")
    extra = ProviderError("could not delete bucket", op="delete")
    error._also_failed = [extra]  # type: ignore[attr-defined]
    assert also_failed(error) == [extra]


def test_ignores_an_attribute_that_is_not_a_list() -> None:
    error = RuntimeError("primary")
    error._also_failed = "not a list"  # type: ignore[attr-defined]
    assert also_failed(error) == []


def test_the_json_renderer_sees_what_attach_recorded() -> None:
    error = AtlantideError("primary")
    attach_also_failed(error, [ProviderError("could not delete bucket", op="delete")])
    assert error_json(error)["error"]["also_failed"] == [
        {"kind": "ProviderError", "message": "could not delete bucket"}
    ]
