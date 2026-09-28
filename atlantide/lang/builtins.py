"""The Atlas-lang global namespace: safe builtins, pure derived functions, and
the sanctioned ``atlantide`` config API.

No clock, randomness, environment, network, or filesystem access exists in the
language. Every function is deterministic given its arguments and the declared
``inputs`` map.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import unicodedata
import uuid
from typing import Any, override

from atlantide.core.errors import LanguageError
from atlantide.core.types import SecretRef, concat, interpolate, join
from atlantide.util.jsonfmt import compact_json

# Fixed namespace so uuid5() is stable across machines and runs.
_ATLAS_NS = uuid.uuid5(uuid.NAMESPACE_URL, "atlantide")

_MISSING = object()


def uuid5(namespace: str, name: str) -> str:
    """Deterministic name-based UUID (RFC 4122 v5)."""
    ns = uuid.uuid5(_ATLAS_NS, namespace)
    return str(uuid.uuid5(ns, name))


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def b64encode(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def b64decode(value: str) -> str:
    return base64.b64decode(value.encode("ascii")).decode("utf-8")


def hmac_sha256_hex(key: str, message: str) -> str:
    """HMAC-SHA256 hex digest of ``message`` under ``key``, e.g. to sign webhook tokens."""
    return hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def to_json(value: Any) -> str:
    """Canonical JSON: sorted keys, no insignificant whitespace.

    The output is byte-stable across runs, so it can be hashed or embedded in
    policy documents.
    """
    return compact_json(value, ascii=False)


def from_json(text: str) -> Any:
    """Parse a JSON document into plain data (dicts, lists, scalars)."""
    return json.loads(text)


def merge(*mappings: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge dicts left to right; later values win.

    Nested dicts merge recursively; any other value is replaced. Inputs are not
    mutated.
    """
    result: dict[str, Any] = {}
    for mapping in mappings:
        for key, value in mapping.items():
            existing = result.get(key)
            if isinstance(existing, dict) and isinstance(value, dict):
                result[key] = merge(existing, value)
            else:
                result[key] = value
    return result


_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """DNS- and resource-name-safe slug: ``"Café Menu!" -> "cafe-menu"``.

    ASCII-folds, lowercases, collapses each run of non-alphanumerics to a single
    ``-`` and trims leading and trailing ``-``.
    """
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return _SLUG_STRIP.sub("-", ascii_value.lower()).strip("-")


class ConfigAPI:
    """The ``atlantide`` handle available to config: sanctioned inputs only.

    State is underscore-prefixed so Atlas-lang cannot read it: ``_inputs`` would
    expose every input without recording it as consumed, and ``_consumed`` would
    let config rewrite the recorded inputs. The engine reads ``_consumed`` through
    :func:`consumed_inputs`.
    """

    def __init__(self, inputs: dict[str, Any]) -> None:
        self._inputs = inputs
        #: Inputs this evaluation read, defaults included. Only these are part of
        #: the plan's identity.
        self._consumed: dict[str, Any] = {}

    @override
    def __repr__(self) -> str:
        # The default repr embeds a memory address; `f"{atlantide!r}"` in a resource
        # field would make the IR hash differ on every run.
        return "<atlantide config API>"

    def input(self, name: str, default: Any = _MISSING) -> Any:
        """A declared config input: a per-run value the config may branch on.

        Determinism holds over *(config, inputs)*: two runs with the same inputs
        produce byte-identical IR. A value that reaches a resource field is part of
        the hashed IR.

        Not for secrets; see :meth:`secret`.
        """
        if name in self._inputs:
            self._consumed[name] = self._inputs[name]
            return self._inputs[name]
        if default is _MISSING:
            raise LanguageError(
                f"required input {name!r} not provided — pass -var {name}=<value>, "
                f"put it in [inputs] in atlantide.toml, or give it a default"
            )
        self._consumed[name] = default
        return default

    def secret(self, name: str, provider: str | None = None) -> SecretRef:
        """A handle to a secret in the configured store, not its value.

        Returns a :class:`~atlantide.core.SecretRef`: only the *name* reaches the
        IR, the artifact and state. The plaintext is resolved in memory at apply
        time and is not persisted.

        It does not read ``inputs``: a value there would reach the hashed IR, the
        ``.atlas`` artifact and the state store. Secrets belong in the store
        (``atlantide secret set``).
        """
        return SecretRef(name, provider=provider)

    # The pure helpers, also reachable as `atlantide.<name>`.
    uuid5 = staticmethod(uuid5)
    sha256_hex = staticmethod(sha256_hex)
    hmac_sha256_hex = staticmethod(hmac_sha256_hex)
    b64encode = staticmethod(b64encode)
    b64decode = staticmethod(b64decode)
    to_json = staticmethod(to_json)
    from_json = staticmethod(from_json)
    merge = staticmethod(merge)
    slugify = staticmethod(slugify)


def consumed_inputs(api: ConfigAPI) -> dict[str, Any]:
    """A copy of the inputs ``api`` handed to config, defaults included."""
    return dict(api._consumed)


# Deterministic subset of Python builtins. The interpreter normalises set
# iteration order.
SAFE_BUILTINS: dict[str, Any] = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "divmod": divmod,
    "enumerate": enumerate,
    "filter": filter,
    "float": float,
    "frozenset": frozenset,
    "int": int,
    "len": len,
    "list": list,
    "map": map,
    "max": max,
    "min": min,
    "range": range,
    "reversed": reversed,
    "round": round,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "zip": zip,
    "True": True,
    "False": False,
    "None": None,
}


def build_globals(inputs: dict[str, Any] | None = None) -> dict[str, Any]:
    """Assemble the global namespace for one config evaluation."""
    scope: dict[str, Any] = dict(SAFE_BUILTINS)
    scope["uuid5"] = uuid5
    scope["sha256_hex"] = sha256_hex
    scope["hmac_sha256_hex"] = hmac_sha256_hex
    scope["b64encode"] = b64encode
    scope["b64decode"] = b64decode
    scope["to_json"] = to_json
    scope["from_json"] = from_json
    scope["merge"] = merge
    scope["slugify"] = slugify
    # Deferred output combinators: compute over apply-time (Ref) values as data.
    scope["concat"] = concat
    scope["interpolate"] = interpolate
    scope["join"] = join
    scope["atlantide"] = ConfigAPI(inputs or {})
    return scope
