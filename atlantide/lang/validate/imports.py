"""The import allow-list: which modules config may import.

The validator applies it to the import statement's spelling; the interpreter
re-applies it where it binds the name (``interp.statements``) and checks the bound
object itself (``interp.binding``).
"""

from __future__ import annotations

from dataclasses import dataclass

_IMPORT_PREFIX = "atlantide"

# Allow-list of the public config surface. Modules outside these packages
# (`atlantide.secrets`, `.state`, `.cli`, `.reconcile`, ...) import stdlib IO
# (`os`, `subprocess`, `pathlib`); binding those into config scope is a sandbox
# escape.
_ALLOWED_IMPORT_PREFIXES: tuple[str, ...] = (
    "atlantide.core",
    "atlantide.policy",
    "atlantide.providers",
    "atlantide.components",
)

# Modules under an allowed prefix that reach IO, nondeterminism or engine state:
# `components.fetch`/`.lock`/`.source` are component fetching (`fetch` shells out
# to git, `lock` reads the filesystem); `core.logging` hands out live
# `logging.Logger`s (their handlers' streams) and can reconfigure redaction;
# `core.plugin` and `providers.loader` discover and build provider plugins
# (`discover()` imports every advertised entry point, `PLUGIN.factory({})` returns
# a live Provider); `core.tuning` reads `os.cpu_count()`; `providers.local.paths`
# resolves real `pathlib.Path`s from the cwd; `providers.aws.config` builds
# botocore client config. `provider`/`handlers` submodules, which hold the boto3,
# filesystem and `uuid`/`token_hex` calls, are covered by
# `_FORBIDDEN_IMPORT_SEGMENTS`.
_FORBIDDEN_IMPORT_MODULES: frozenset[str] = frozenset(
    {
        "atlantide.components.fetch",
        "atlantide.components.lock",
        "atlantide.components.source",
        "atlantide.core.logging",
        "atlantide.core.plugin",
        "atlantide.core.tuning",
        "atlantide.providers.loader",
        "atlantide.providers.local.paths",
        "atlantide.providers.aws.config",
    }
)

#: Public `atlantide.core` names that are engine machinery, not config surface.
#: Binding one gives config the live evaluation state (the resource registry, the
#: `--env` selection) outside the declaration path. Rejected here by name from any
#: `atlantide.*` module, since submodules and provider/component packages can
#: re-export them, and by object identity where the interpreter binds the name
#: (`interp.binding._forbidden_objects`).
FORBIDDEN_CORE_NAMES: frozenset[str] = frozenset(
    {
        # The live, mutable ResourceRegistry of the running evaluation.
        "active_registry",
        # Opens a fresh registry scope: config could swap the collector out.
        "collecting",
        # The registry type itself; resources register by being declared.
        "ResourceRegistry",
        # Post-evaluation engine pass rewriting a registry's stack outputs.
        "inline_stack_outputs",
        # Installs a new `--env` selection for the evaluation.
        "selecting",
        # The run's mutable EnvSelection (`claim()`, `consumed`).
        "current_selection",
        # The selection record type; config reads envs through `Config.envs()`.
        "EnvSelection",
        # Provider registry and its version-pinning helpers: the CLI drives providers.
        "ProviderRegistry",
        "check_compatible",
        "parse_semver",
        # Per-call context handed to provider CRUD; config never calls a provider.
        "Context",
        # The provider base class (the interpreter also rejects any subclass).
        "Provider",
    }
)

# Dotted segments marking an internal module anywhere under the allowed prefixes
# (`atlantide.providers.aws.provider`, `...aws.handlers.s3`).
_FORBIDDEN_IMPORT_SEGMENTS: frozenset[str] = frozenset({"provider", "handlers"})


def _is_private_segment(segment: str) -> bool:
    """Whether ``segment`` names a private (leading-underscore) module.

    A private module (``atlantide.core._describe``) is package implementation, not
    config surface. Its definitions stay unreachable through a public re-export:
    the interpreter checks where a bound object was *defined* (``interp.binding``).
    """
    return segment.startswith("_")


#: Rendered in import rejections so the message names the surface, not the rule.
ALLOWED_IMPORTS_DESC = "'atlantide.core', '.policy', '.providers.*', '.components.*'"


def private_import_message(name: str, module: str) -> str:
    """Rejection text for a leading-underscore import, shared with the interpreter."""
    return f"cannot import private name {name!r} from {module!r}; config imports only public API"


def engine_import_message(name: str, module: str) -> str:
    """Rejection text for binding engine machinery, shared with the interpreter."""
    return (
        f"cannot import {name!r} from {module!r}; it is engine machinery, not "
        f"config API — config declares resources, the engine collects them"
    )


def is_atlantide_module(module: str) -> bool:
    return module == _IMPORT_PREFIX or module.startswith(_IMPORT_PREFIX + ".")


@dataclass(frozen=True, slots=True)
class LanguageSurface:
    """Which modules config may import.

    The built-in prefixes plus those installed provider plugins contribute. Passed
    explicitly rather than held in a module global, so validation does not depend
    on load order.

    Third-party modules follow the same internal-module rules as the built-ins: a
    plugin's ``provider``/``handlers`` submodules hold its network and filesystem
    calls, and binding them into config scope is a sandbox escape.
    """

    extra: frozenset[str] = frozenset()

    def prefixes(self) -> tuple[str, ...]:
        return (*_ALLOWED_IMPORT_PREFIXES, *sorted(self.extra))


#: The surface with no plugins loaded: the shipped providers only.
DEFAULT_SURFACE = LanguageSurface()


def import_allowed(module: str | None, surface: LanguageSurface = DEFAULT_SURFACE) -> bool:
    if not module:
        return False
    if module == _IMPORT_PREFIX:
        return True
    if not any(
        module == prefix or module.startswith(prefix + ".") for prefix in surface.prefixes()
    ):
        return False
    if any(module == m or module.startswith(m + ".") for m in _FORBIDDEN_IMPORT_MODULES):
        return False
    segments = module.split(".")
    if any(_is_private_segment(segment) for segment in segments):
        return False
    return not _FORBIDDEN_IMPORT_SEGMENTS.intersection(segments)
