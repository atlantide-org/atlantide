"""Repo-wide pytest configuration.

The shared builders live in :mod:`tests.support`; ``make_engine`` is re-exported
here because that is where suites have always imported it from.

Also registers the Hypothesis profiles. The default profile is **derandomized**:
a project whose headline claim is that the same config always produces the same
plan should not have a test suite that passes or fails on an unseeded PRNG. A
property that only fails one run in twenty is worse than no property at all —
it teaches people the suite is flaky, and the next real failure gets re-run
instead of read.
"""

from __future__ import annotations

import os

from hypothesis import HealthCheck, Verbosity, settings

from tests.support.factories import make_engine

__all__ = ["make_engine"]

#: Shared by every profile. The default 200 ms deadline is measured per example
#: and trips on a loaded CI runner for reasons that have nothing to do with the
#: code under test; a generous explicit budget still catches genuine blowups.
_COMMON = {
    "deadline": 2000,
    "suppress_health_check": [HealthCheck.too_slow],
}

settings.register_profile("dev", max_examples=50, **_COMMON)  # type: ignore[arg-type]
settings.register_profile("ci", max_examples=100, derandomize=True, **_COMMON)  # type: ignore[arg-type]
settings.register_profile(
    "thorough",  # opt-in soak: HYPOTHESIS_PROFILE=thorough uv run pytest
    max_examples=1000,
    verbosity=Verbosity.verbose,
    **_COMMON,  # type: ignore[arg-type]
)
settings.register_profile(
    # The nightly job (`.github/workflows/soak.yml`). `thorough`'s example count
    # without its per-example transcript: `verbose` is for a human watching one
    # property fail, and at a thousand examples across the whole suite it buries
    # the failure it was meant to explain. `print_blob` replaces it — an
    # unattended run has to hand back something replayable, since it is not
    # derandomized and nobody will reproduce it by re-running.
    "soak",
    max_examples=1000,
    print_blob=True,
    **_COMMON,  # type: ignore[arg-type]
)

settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))
