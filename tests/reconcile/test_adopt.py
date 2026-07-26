"""Adopting an existing resource: the row written has to be the row apply writes.

The headline is :func:`test_an_imported_node_plans_as_a_noop`. Everything else in
this file exists because there are several ways to write a row that *looks* right
and is not — a resolved ``$ref`` where a marker belonged, a reported input filed
as an output, a hash recomputed rather than reused — and each of them is silent
until a later plan does something surprising.
"""

from __future__ import annotations

from typing import Any

import pytest

from atlantide.core.errors import LeaseLostError
from atlantide.reconcile import Action, ImportRequest
from atlantide.reconcile.adopt import ImportStatus
from atlantide.state.backend import NO_INPUT_HASH, STATUS_CREATED
from tests.support import Bucket, FakeProvider, Harness, Notifier
from tests.support.resources import Box

BOX = "Box('b', size=1, label='hi')\n"
#: Node ids are stack-qualified, and state is keyed by them.
BOX_ID = "default:test.Box:b"
BUCKET_ID = "default:test.Bucket:b"
NOTIFIER_ID = "default:test.Notifier:n"

#: A bucket and a notifier that reads its arn — the smallest graph with an edge.
LINKED = "b = Bucket('b', bucket_name='n')\nNotifier('n', target_arn=b.arn)\n"


def _harness(live: dict[str, dict[str, Any] | None], **kw: Any) -> Harness:
    return Harness.of(Box, Bucket, Notifier, provider=FakeProvider(live=live), **kw)


# -- the point of the feature -------------------------------------------------


def test_an_imported_node_plans_as_a_noop() -> None:
    """Import, then plan: nothing to do.

    This is the whole feature in one assertion. A row whose ``input_hash`` is not
    the compile's Merkle hash still *works* — it just plans as an UPDATE forever,
    which is the failure this guards.
    """
    harness = _harness({"b": {"out": "b:1"}})
    [outcome] = harness.adopt(BOX, BOX_ID)
    assert outcome.status is ImportStatus.IMPORTED

    assert [c.action for c in harness.diff_only(BOX).changes] == [Action.NOOP]


def test_the_row_matches_what_an_apply_would_have_written() -> None:
    """Compare the two rows directly, rather than trusting a field list here to
    stay in step with the one the executor writes."""
    imported = _harness({"b": {"out": "b:1"}})
    imported.adopt(BOX, BOX_ID)
    applied = Harness.of(Box, Bucket, Notifier, provider=FakeProvider())
    applied.apply(BOX)

    row = imported.backend.load().nodes[BOX_ID]
    expected = applied.backend.load().nodes[BOX_ID]
    assert row.input_hash == expected.input_hash
    assert row.properties == expected.properties
    assert row.dependencies == expected.dependencies
    assert row.type == expected.type
    assert row.status == STATUS_CREATED == expected.status


# -- how the row is built -----------------------------------------------------


def test_a_ref_stays_symbolic_in_the_adopted_row() -> None:
    """Recording the value a ``$ref`` resolved to would erase the dependency from
    state, and the next config change would diff a marker against a literal — a
    REPLACE on a field nobody touched."""
    harness = _harness({"b": {"arn": "arn:b"}, "n": {}})
    outcomes = harness.adopt(LINKED, BUCKET_ID, NOTIFIER_ID)
    assert [o.status for o in outcomes] == [ImportStatus.IMPORTED, ImportStatus.IMPORTED]

    row = harness.backend.load().nodes[NOTIFIER_ID]
    assert row.properties["target_arn"] == {"$ref": f"{BUCKET_ID}#arn"}
    assert row.dependencies == (BUCKET_ID,)


def test_a_reported_input_is_not_filed_as_an_output() -> None:
    """A read reports inputs and computed values in one mapping. Filing a reported
    input as an output shadows the input it mirrors, and every later refresh then
    compares the value against itself."""
    source = "Bucket('b', bucket_name='n', versioning=True)\n"
    harness = _harness({"b": {"arn": "arn:b", "versioning": True}})
    [outcome] = harness.adopt(source, BUCKET_ID)
    assert outcome.status is ImportStatus.IMPORTED

    row = harness.backend.load().nodes[BUCKET_ID]
    assert "versioning" not in row.outputs
    assert row.outputs["arn"] == "arn:b"
    assert outcome.recorded == ("arn",)


def test_a_sensitive_value_is_never_echoed_in_a_drift_report() -> None:
    """Import prints what differs, and a differing secret is still a secret.

    ``token`` is a sensitive *input*, so a live value that disagrees with config
    is drift rather than something to record — and the report has to name the
    field without printing either side of it.
    """
    source = "Bucket('b', bucket_name='n', token='declared')\n"
    harness = _harness({"b": {"arn": "arn:b", "token": "actual"}})
    [outcome] = harness.adopt(source, BUCKET_ID)

    assert outcome.status is ImportStatus.DRIFTED
    assert outcome.drift is not None
    assert outcome.drift.changed["token"] == ("(sensitive)", "(sensitive)")
    assert "declared" not in str(outcome.drift)
    assert "actual" not in str(outcome.drift)


def test_a_batch_adopts_in_dependency_order() -> None:
    """The downstream read has to see a resolved value, not an unresolved ref —
    which is only true if its dependency was adopted first."""
    harness = _harness({"b": {"arn": "arn:b"}, "n": {}})
    harness.adopt(LINKED, BUCKET_ID, NOTIFIER_ID)
    assert harness.fake().input("read", "n").target_arn == "arn:b"


# -- refusals -----------------------------------------------------------------


def test_a_read_that_finds_nothing_writes_no_row() -> None:
    harness = _harness({"b": None})
    [outcome] = harness.adopt(BOX, BOX_ID)
    assert outcome.status is ImportStatus.NOT_FOUND
    assert harness.backend.load().nodes == {}


def test_a_node_already_in_state_is_left_alone() -> None:
    harness = _harness({"b": {"out": "b:1"}})
    harness.adopt(BOX, BOX_ID)
    before = harness.backend.load().nodes[BOX_ID]

    [outcome] = harness.adopt(BOX, BOX_ID)
    assert outcome.status is ImportStatus.ALREADY_TRACKED
    assert harness.backend.load().nodes[BOX_ID] == before


def test_force_re_adopts_a_tracked_node() -> None:
    harness = _harness({"b": {"out": "b:1"}})
    harness.adopt(BOX, BOX_ID)
    [outcome] = harness.adopt(BOX, BOX_ID, force=True)
    assert outcome.status is ImportStatus.IMPORTED


def test_a_node_not_in_this_config_is_blocked() -> None:
    harness = _harness({})
    [outcome] = harness.adopt(BOX, "default:test.Box:nope")
    assert outcome.status is ImportStatus.BLOCKED
    assert "not in this config" in outcome.detail


def test_a_node_whose_dependency_is_absent_is_refused_by_name() -> None:
    """Adopting the notifier first would read a resource whose inputs are half
    unresolved — matching nothing, or something unrelated."""
    harness = _harness({"b": {"arn": "arn:b"}, "n": {}})
    [outcome] = harness.adopt(LINKED, NOTIFIER_ID)
    assert outcome.status is ImportStatus.BLOCKED
    assert BUCKET_ID in outcome.detail
    assert harness.backend.load().nodes == {}


def test_a_failed_request_does_not_stop_the_ones_after_it() -> None:
    """A partial batch is resumable; aborting on the first problem means finding
    the problems one run at a time."""
    source = BOX + "Bucket('gone', bucket_name='n')\n"
    harness = _harness({"b": {"out": "b:1"}, "gone": None})
    outcomes = harness.adopt(source, "default:test.Bucket:gone", BOX_ID)
    assert [o.status for o in outcomes] == [ImportStatus.NOT_FOUND, ImportStatus.IMPORTED]
    assert set(harness.backend.load().nodes) == {BOX_ID}


# -- drift --------------------------------------------------------------------

_DRIFTED = "Bucket('b', bucket_name='n', versioning=True)\n"


def test_drift_is_refused_and_writes_nothing() -> None:
    """An import that silently means "your next apply will change your
    infrastructure" is not a success worth reporting as one."""
    harness = _harness({"b": {"arn": "arn:b", "versioning": False}})
    [outcome] = harness.adopt(_DRIFTED, BUCKET_ID)
    assert outcome.status is ImportStatus.DRIFTED
    assert outcome.drift is not None
    assert "versioning" in outcome.drift.changed
    assert harness.backend.load().nodes == {}


def test_allow_drift_poisons_the_hash_so_the_next_plan_sees_it() -> None:
    """Config and state hash identically after a drifted adopt — the diff is
    symbolic — so clearing the hash is the only channel the next plan has."""
    harness = _harness({"b": {"arn": "arn:b", "versioning": False}})
    [outcome] = harness.adopt(_DRIFTED, BUCKET_ID, allow_drift=True)
    assert outcome.status is ImportStatus.IMPORTED
    assert harness.backend.load().nodes[BUCKET_ID].input_hash == NO_INPUT_HASH


def test_a_drifted_adopt_plans_as_an_update_not_a_replace() -> None:
    """The poisoned hash reaches the diff with matching symbolic properties, which
    is the branch yielding an UPDATE. A REPLACE here would destroy the resource
    the user had just adopted."""
    harness = _harness({"b": {"arn": "arn:b", "versioning": False}})
    harness.adopt(_DRIFTED, BUCKET_ID, allow_drift=True)
    assert [c.action for c in harness.diff_only(_DRIFTED).changes] == [Action.UPDATE]


# -- dry run and locking ------------------------------------------------------


def test_a_dry_run_writes_nothing_but_still_reports() -> None:
    harness = _harness({"b": {"out": "b:1"}})
    [outcome] = harness.adopt(BOX, BOX_ID, write=False)
    assert outcome.status is ImportStatus.WOULD_IMPORT
    assert outcome.recorded == ("out",)
    assert harness.backend.load().nodes == {}


def test_a_dry_run_resolves_dependencies_like_the_real_run() -> None:
    """A dry run must answer as the real run would: a request depending on an
    earlier one in the same batch is importable, not BLOCKED — the earlier
    would-be import counts as tracked, and its outputs resolve the ``$ref``."""
    harness = _harness({"b": {"arn": "arn:b"}, "n": {}})
    outcomes = harness.adopt(LINKED, BUCKET_ID, NOTIFIER_ID, write=False)
    assert [o.status for o in outcomes] == [
        ImportStatus.WOULD_IMPORT,
        ImportStatus.WOULD_IMPORT,
    ]
    assert harness.backend.load().nodes == {}
    # the downstream read still saw the resolved upstream value
    assert harness.fake().input("read", "n").target_arn == "arn:b"


def test_a_lost_lease_refuses_the_write() -> None:
    """Losing the lease cancels a run, but not instantly; a node already past its
    provider call must not still write."""
    harness = _harness({"b": {"out": "b:1"}})
    harness.lease.fail(LeaseLostError("lease lost"))
    with pytest.raises(LeaseLostError):
        harness.adopt(BOX, BOX_ID)
    assert harness.backend.load().nodes == {}


# -- identity fields ----------------------------------------------------------


def test_a_name_addressed_type_refuses_an_external_id() -> None:
    """The test provider declares no identity field, so an id passed here is a
    misunderstanding worth naming rather than silently dropping."""
    harness = _harness({"b": {"out": "b:1"}})
    [outcome] = harness.adopt(BOX, ImportRequest(BOX_ID, external_id="vpc-123"))
    assert outcome.status is ImportStatus.BLOCKED
    assert "takes no id" in outcome.detail


def test_an_id_field_override_seeds_the_computed_field_before_the_read() -> None:
    """``--id-field`` is the escape hatch for a type whose read keys on something
    the provider does not declare. The seeded value has to reach the resource the
    provider is handed, or the override does nothing at all."""
    source = "Bucket('b', bucket_name='n')\n"
    harness = _harness({"b": {"arn": "arn:seeded"}})
    [outcome] = harness.adopt(
        source, ImportRequest(BUCKET_ID, external_id="arn:seeded", id_field="arn")
    )
    assert outcome.status is ImportStatus.IMPORTED
    assert harness.fake().seen_values("arn", "read") == ["arn:seeded"]
