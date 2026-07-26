"""What an apply says when things went wrong on the way out.

These are the lines that tell an operator state no longer describes reality, and
they had no test at all — which is how a "simplification" of the block that emits
them could have changed the wording, the styling, or dropped one entirely without
anything noticing.

Asserted as exact strings rather than fragments. The wording *is* the feature: it
is the only place a half-completed rollback, a state row that could not be marked
stale, or a resource left running with no state row is ever explained. Editing one
should be a deliberate act that updates this file too.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from atlantide.cli import render
from atlantide.reconcile import ApplyReport


def _rendered(report: ApplyReport, **kwargs: object) -> str:
    """The report as plain text, wide enough that nothing wraps."""
    buffer = io.StringIO()
    original = render.console
    render.console = Console(file=buffer, width=200, force_terminal=False)
    try:
        render.render_report(report, show_nodes=False, **kwargs)  # type: ignore[arg-type]
    finally:
        render.console = original
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
    """The worst of the three: the next plan reports no change for a resource
    whose state is known to be wrong, and nothing else will say so."""
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
    assert render.SECRET_REDACTED in output


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
