"""Atlas-lang subset validation.

Parses source with the stdlib ``ast`` (every config file is valid Python) and
rejects any construct outside the allowed subset before evaluation, enforcing
determinism by construction. See ``lang/README.md`` for the module map.
"""

from __future__ import annotations

from atlantide.lang.validate.imports import (
    DEFAULT_SURFACE,
    FORBIDDEN_CORE_NAMES,
    LanguageSurface,
    engine_import_message,
    import_allowed,
    private_import_message,
)
from atlantide.lang.validate.rules import attribute_rejection
from atlantide.lang.validate.schema import ENV_SCHEMA_BASE
from atlantide.lang.validate.validator import validate_source

__all__ = [
    "DEFAULT_SURFACE",
    "ENV_SCHEMA_BASE",
    "FORBIDDEN_CORE_NAMES",
    "LanguageSurface",
    "attribute_rejection",
    "engine_import_message",
    "import_allowed",
    "private_import_message",
    "validate_source",
]
