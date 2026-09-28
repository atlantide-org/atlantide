"""What an apply says when things went wrong on the way out.

These are the lines that tell an operator state no longer describes reality.
Without a test, a change to the block that emits them could alter the wording or
styling, or drop a line, unnoticed.

Asserted as exact strings rather than fragments. The wording *is* the feature: it
is the only place a half-completed rollback, a state row that could not be marked
stale, or a resource left running with no state row is explained. Changing the
wording means updating this file too.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from rich.console import Console

from atlantide.cli.views import common, report
from atlantide.reconcile import ApplyReport


@contextmanager
def _captured() -> Iterator[io.StringIO]:
    """Route both modules the report prints through — its own lines and the
    shared per-stack sections — into one buffer, wide enough that nothing wraps."""
    buffer = io.StringIO()
    capture = Console(file=buffer, width=200, force_terminal=False)
    originals = report.console, common.console
    report.console = common.console = capture
    try:
        yield buffer
    finally:
        report.console, common.console = originals


def _rendered(applied: ApplyReport, **kwargs: object) -> str:
    """The report as plain text, summary and trouble only."""
    with _captured() as buffer:
        report.render_report(applied, show_nodes=False, **kwargs)  # type: ignore[arg-type]
    return buffer.getvalue()


def test_a_clean_run_says_only_what_it_did() -> None:
    """No trouble section appears when there was no trouble — the sections are
    silent when empty, not printed with a zero count."""
    output = _rendered(ApplyReport(created=["s:t:a"]))

    assert output.strip() == "Applied: 1 to add"


def test_a_rollback_is_reported_by_count() -> None:
    output = _rendered(ApplyReport(created=["s:t:a"], rolled_back=["s:t:b", "s:t:c"]))

    assert "rolled back 2 node(s)" in output


def test_a_skipped_rollback_says_the_resources_are_still_there() -> None:
    """Deliberate, not a failure — but silence would read as "nothing happened"."""
    output = _rendered(ApplyReport(rollback_skipped="the lease was lost"))

    assert "rollback skipped — the lease was lost" in output
    assert (
        "resources this run created were left in place; run `atlantide refresh` "
        "to see what exists" in output
    )


def test_a_failed_rollback_names_every_node_and_its_reason() -> None:
    """The operator has to know *which* resources to go and look at."""
    output = _rendered(
        ApplyReport(rollback_failed={"s:t:a": "delete timed out", "s:t:b": "access denied"})
    )

    assert (
        "rollback incomplete for 2 node(s) — state may not describe the live resources:" in output
    )
    assert "s:t:a: delete timed out" in output
    assert "s:t:b: access denied" in output
    assert (
        "these rows are marked stale, so the next plan will re-check them "
        "instead of reporting no change" in output
    )


def test_nodes_that_could_not_be_marked_stale_say_the_next_plan_will_lie() -> None:
    """The next plan reports no change for a resource whose state is known to be
    wrong, and nothing else will say so."""
    output = _rendered(ApplyReport(poison_failed={"s:t:a": "write refused"}))

    assert (
        "1 node(s) could not be marked stale — the next plan will report no change "
        "for them even though state is wrong; run `atlantide refresh` before "
        "applying again:" in output
    )
    assert "s:t:a: write refused" in output


def test_orphans_say_nothing_will_ever_find_them_again() -> None:
    output = _rendered(ApplyReport(orphaned={"s:t:a": "state delete failed"}))

    assert (
        "1 resource(s) left running untracked — atlantide no longer has a state row "
        "for them, so nothing will find them again; delete them by hand:" in output
    )
    assert "s:t:a: state delete failed" in output


def test_every_kind_of_trouble_is_reported_together() -> None:
    """One run can hit several. Reporting only the first would leave an operator
    fixing one problem while another sat unmentioned."""
    output = _rendered(
        ApplyReport(
            created=["s:t:a"],
            rolled_back=["s:t:b"],
            rollback_skipped="lease lost",
            rollback_failed={"s:t:c": "boom"},
            poison_failed={"s:t:d": "write refused"},
            orphaned={"s:t:e": "delete failed"},
        )
    )

    for expected in (
        "rolled back 1 node(s)",
        "rollback skipped — lease lost",
        "rollback incomplete for 1 node(s)",
        "could not be marked stale",
        "left running untracked",
    ):
        assert expected in output, f"missing: {expected}"


def test_a_sensitive_output_is_not_printed() -> None:
    output = _rendered(
        ApplyReport(outputs={"url": "https://x", "token": "hunter2"}, sensitive_outputs={"token"})
    )

    assert "url = https://x" in output
    assert "hunter2" not in output
    assert common.SECRET_REDACTED in output


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_node_rows_are_grouped_under_their_stack() -> None:
    output = _rendered_nodes(ApplyReport(created=["s1:t.A:a", "s2:t.B:b"], deleted=["s1:t.C:c"]))
    lines = [line.rstrip() for line in output.splitlines()]
    assert lines[0].startswith("s1 ")
    assert lines[1:3] == ["  + done t.A:a", "  - done t.C:c"]
    assert lines[3].startswith("s2 ")
    assert lines[4] == "  + done t.B:b"


def _rendered_nodes(applied: ApplyReport) -> str:
    with _captured() as buffer:
        report.render_report(applied)
    return buffer.getvalue()


def test_a_state_only_write_is_counted_apart_from_the_unchanged() -> None:
    output = _rendered(ApplyReport(noop=["s:t:a", "s:t:b"], state_only=["s:t:a"]))

    assert output.strip() == "Applied: 1 state-only, 1 unchanged"


def test_a_replace_the_apply_did_not_need_is_named() -> None:
    output = _rendered(
        ApplyReport(
            updated=["s:t:up", "s:t:cdn"],
            noop=["s:t:pol"],
            downgraded={"s:t:cdn": "update", "s:t:pol": "noop"},
        )
    )

    assert output.splitlines() == [
        "",
        "Applied: 2 to change, 1 unchanged",
        "2 known-after-apply replace(s) not needed — no immutable value changed:",
        "  t:cdn: update instead of replace",
        "  t:pol: noop instead of replace",
    ]


def test_the_node_rows_show_a_state_only_write() -> None:
    with _captured() as buffer:
        report.render_report(ApplyReport(noop=["s:t:a"], state_only=["s:t:a"]))
    assert "~ done t:a  (state only)" in buffer.getvalue()


def test_the_live_table_shows_the_action_a_node_finished_with() -> None:
    """A known-after-apply replace that was not needed finishes as what it ran as."""
    from atlantide.cli.progress import ProgressTable
    from atlantide.core.actions import Action
    from atlantide.reconcile.progress import Phase

    table = ProgressTable([("s:t:cdn", Action.REPLACE)])
    table.record("s:t:cdn", Action.REPLACE, Phase.START)
    assert table._action_of["s:t:cdn"] is Action.REPLACE
    table.record("s:t:cdn", Action.NOOP, Phase.FINISH)
    assert table._action_of["s:t:cdn"] is Action.NOOP
