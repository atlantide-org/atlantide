"""Atlantide: typed Python IaC engine with deterministic Atlas-lang config."""

#: Kept in step with ``[project].version`` in pyproject.toml; the release
#: workflow refuses to publish if the two disagree. Read only when the package
#: is not installed (running from a source tree) — otherwise the CLI reports the
#: installed distribution's metadata.
__version__ = "0.3.0"
