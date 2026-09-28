"""The ``atlantide`` entry point (``atlantide.cli.main:main``).

The app itself is assembled in :mod:`atlantide.cli.app`; it is re-exported here
because the entry point and the test harness import it from this path.
"""

from __future__ import annotations

from atlantide.cli.app import app

__all__ = ["app", "main"]


def main() -> None:
    app()


if __name__ == "__main__":
    main()
