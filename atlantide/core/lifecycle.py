"""Per-instance lifecycle overrides, layered over class-level field mutability."""

from __future__ import annotations

from dataclasses import dataclass

from atlantide.core.node_id import require_sequence


@dataclass(frozen=True, slots=True)
class Lifecycle:
    """Instance-level overrides consumed by diff / planner / executor.

    - ``prevent_destroy`` — a planned DELETE (or a REPLACE's destroy half) on
      this resource fails the whole plan.
    - ``create_before_destroy`` — a REPLACE creates the new resource *before*
      destroying the old one (no downtime). Falls back to destroy-before-create
      when the replacement would collide with the old resource's identity.
    - ``ignore_changes`` — field names whose drift is ignored: excluded from the
      Merkle ``input_hash`` and from the diff's changed-field set, so a change to
      one of them never triggers UPDATE/REPLACE.
    - ``aliases`` — prior node ids (or bare old logical names, resolved against
      this resource's stack + type) this resource has been *renamed from*. When
      the new id is absent from state but an alias id is present, the plan maps
      the existing state node to the new id instead of destroy + create.
    """

    prevent_destroy: bool = False
    create_before_destroy: bool = False
    ignore_changes: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Accept any iterable (e.g. a list literal in config) but always store a
        # canonical tuple, so equality/hashing stay stable.
        for field in ("ignore_changes", "aliases"):
            value = getattr(self, field)
            # A bare string would tuple into characters: the alias matches no
            # state node and the rename lowers to a destroy plus a create.
            require_sequence(
                value,
                f"Lifecycle.{field} must be a sequence of names, not the string {value!r}",
                f"use a tuple or list, e.g. ({value!r},)",
            )
            if not isinstance(value, tuple):
                object.__setattr__(self, field, tuple(value))
