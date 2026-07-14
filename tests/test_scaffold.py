"""Package sanity."""

from __future__ import annotations

import atlantide


def test_version() -> None:
    assert atlantide.__version__
