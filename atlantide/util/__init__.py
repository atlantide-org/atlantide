"""Internal plumbing shared by the engine-side packages.

Not part of the config API. ``atlantide.util`` is outside the import allow-list
in :mod:`atlantide.lang.validate`: these helpers touch the filesystem and build
AWS clients, so importing them from config would escape the sandbox. ``util``
depends only on ``atlantide.core`` (enforced by import-linter).
"""

__all__: list[str] = []
