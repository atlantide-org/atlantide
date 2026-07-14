"""The node-id format ``{stack}:{type}:{name}`` — one place to build and parse it.

Every resource is identified by this triple; state rows, IR nodes, locks, and
CLI output all key on it.
"""

from __future__ import annotations

import re

from atlantide.core.errors import RegistryError

#: Resource and stack names: a letter, then letters/digits/``_``/``-``.
IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def require_identifier(name: str, kind: str) -> None:
    """Validate a resource/stack name, raising :class:`RegistryError` if invalid."""
    if not IDENTIFIER_RE.match(name):
        raise RegistryError(
            f"invalid {kind} name {name!r}: must match {IDENTIFIER_RE.pattern}"
        )


def format_node_id(stack: str, type_name: str, name: str) -> str:
    """Compose a node id from its parts."""
    return f"{stack}:{type_name}:{name}"


def stack_of(node_id: str) -> str:
    """The stack component (everything before the first ``:``)."""
    return node_id.partition(":")[0]


def type_name_of(node_id: str) -> str:
    """The resource-type component (the middle of ``{stack}:{type}:{name}``)."""
    return node_id.split(":", 2)[1]


def local_name_of(node_id: str) -> str:
    """The logical-name component (everything after the last ``:``)."""
    return node_id.rsplit(":", 1)[-1]


def short_id(node_id: str) -> str:
    """The id without its stack prefix (``{type}:{name}``), for grouped display."""
    return node_id.partition(":")[2]


def group_by_stack(node_ids: list[str]) -> dict[str, list[str]]:
    """Group node ids by their stack, preserving first-seen order."""
    grouped: dict[str, list[str]] = {}
    for node_id in node_ids:
        grouped.setdefault(stack_of(node_id), []).append(node_id)
    return grouped


def field_scope(node_id: str, field: str) -> str:
    """The per-field scope string ``{node_id}:{field}``.

    The secret-rotation digest is keyed by this scope; the planner (detection)
    and executor (recording) must build it identically.
    """
    return f"{node_id}:{field}"
