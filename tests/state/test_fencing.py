"""The refusal wording, pinned: operators grep for it, and every backend shares it."""

from __future__ import annotations

import pytest

from atlantide.core.errors import FencedWriteError, StateError
from atlantide.state import fencing
from atlantide.state.leases import Lease, require_skew_margin


def _lease(**overrides: object) -> Lease:
    fields: dict[str, object] = {
        "owner": "me",
        "expires_at": 100.0,
        "scope": frozenset({"a"}),
        "fence": 3,
    }
    fields.update(overrides)
    return Lease(**fields)  # type: ignore[arg-type]


def _refusal(held: dict[str, Lease], now: float = 50.0, node: str = "a") -> str:
    error = fencing.fence_violation(_lease(), held, now, {node})
    assert isinstance(error, FencedWriteError)
    return str(error)


def test_a_write_outside_the_scope_is_worded_once() -> None:
    assert _refusal({}, node="b") == (
        "refusing to write 'b': it is outside this run's lock scope, so nothing was protecting it"
    )


def test_a_hold_taken_by_another_owner() -> None:
    assert _refusal({"a": _lease(owner="other")}) == (
        "refusing to write 'a': the state lock is now held by 'other', not by this run. "
        "Resources this run created exist but are not recorded; run `atlantide refresh` "
        "before applying again"
    )


def test_a_hold_superseded_by_the_same_owner() -> None:
    assert _refusal({"a": _lease(fence=4)}) == (
        "refusing to write 'a': this run's lease (fence 3) was superseded by a newer one "
        "(fence 4) taken by the same owner"
    )


def test_a_lapsed_hold() -> None:
    assert _refusal({"a": _lease()}, now=100.0) == (
        "refusing to write 'a': this run's lease expired. Run `atlantide refresh` before "
        "applying again"
    )


def test_the_s3_variants() -> None:
    """S3 decides at its heads, so it words the same situations with its own detail."""
    assert fencing.held_by_another("a", "other", detail=" (fence 9 superseded 3)") == (
        "refusing to write 'a': the state lock is now held by 'other', not by this run "
        "(fence 9 superseded 3). Resources this run created exist but are not recorded; "
        "run `atlantide refresh` before applying again"
    )
    assert fencing.superseded("a", 3, 9, taker="another run", refresh=True) == (
        "refusing to write 'a': this run's lease (fence 3) was superseded by a newer one "
        "(fence 9) taken by another run. Run `atlantide refresh` before applying again"
    )
    assert fencing.never_recorded("a", 3, "s3://b/k", 1) == (
        "refusing to write 'a': this run's lease (fence 3) was never recorded in s3://b/k "
        "(its head is at fence 1), so it was never safe to write under"
    )


def test_a_negative_skew_margin_is_refused_and_zero_is_not() -> None:
    assert require_skew_margin(0.0) == 0.0
    with pytest.raises(StateError, match=r"lock_skew_margin must not be negative, got -1\.0"):
        require_skew_margin(-1.0)
