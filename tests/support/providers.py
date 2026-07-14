"""FakeProvider: one configurable in-memory provider for tests.

Subsumes every ad-hoc mock the suite used to define. It records each call and
the resource seen, generates outputs (static dict, per-op callable, or a derived
default), injects failures, and can serve pre-seeded reads for drift tests.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

from atlantide.core import Context, Provider, Resource

#: Output for one op: a fixed dict, a callable of ``(ctx, res)``, or ``None`` to
#: fall back to :func:`default_outputs`.
OutputSpec = dict[str, Any] | Callable[[Context, Resource], dict[str, Any]] | None


def default_outputs(op: str, res: Resource) -> dict[str, Any]:
    """Derive ``{"out": "<name>:<size>"}`` (``:u`` on update) for resources with a
    ``size`` field; ``{}`` otherwise. Reproduces the old reconcile MockProvider."""
    size = getattr(res, "size", None)
    if size is None:
        return {}
    base = f"{res.logical_name}:{size}"
    return {"out": f"{base}:u"} if op == "update" else {"out": base}


class FakeProvider(Provider):
    """A configurable provider. Construct one per suite; inspect ``calls``/``seen``.

    ``name``/``version`` set this provider's registry identity. ``on_create``/
    ``on_update``/``on_read`` control outputs per op (default: :func:`default_outputs`).
    ``live`` pre-seeds ``read`` by logical name (``None`` value = absent/deleted).
    ``fail_*`` sets inject a ``RuntimeError`` for the named resources.
    """

    # Instance-level identity overriding the base ClassVars (a fresh provider per
    # suite needs its own name/version; the registry reads ``provider.name``).
    # Default "test" matches the canonical resources in tests.support.resources.
    name: ClassVar[str] = "test"
    version: ClassVar[str] = "1.0.0"

    def __init__(
        self,
        *,
        name: str = "test",
        version: str = "1.0.0",
        on_create: OutputSpec = None,
        on_update: OutputSpec = None,
        on_read: OutputSpec = None,
        live: dict[str, dict[str, Any] | None] | None = None,
        fail_create: set[str] | None = None,
        fail_update: set[str] | None = None,
        fail_delete: set[str] | None = None,
        fail_read: set[str] | None = None,
    ) -> None:
        # Instance identity overriding the base ClassVars (each suite's provider
        # sets its own name/version; the registry reads ``provider.name``).
        self.name = name  # type: ignore[misc]
        self.version = version  # type: ignore[misc]
        self._on = {"create": on_create, "update": on_update, "read": on_read}
        self._live = live
        # Public mutable failure sets — add/clear logical names to inject errors.
        self.fail_create: set[str] = fail_create or set()
        self.fail_update: set[str] = fail_update or set()
        self.fail_delete: set[str] = fail_delete or set()
        self.fail_read: set[str] = fail_read or set()
        self.calls: list[tuple[str, str]] = []
        self.seen: list[tuple[str, Resource]] = []

    # -- inspection -------------------------------------------------------

    def reset(self) -> None:
        self.calls.clear()
        self.seen.clear()

    def _names(self, op: str) -> list[str]:
        return [name for o, name in self.calls if o == op]

    @property
    def created(self) -> list[str]:
        return self._names("create")

    @property
    def updated(self) -> list[str]:
        return self._names("update")

    @property
    def deleted(self) -> list[str]:
        return self._names("delete")

    @property
    def reads(self) -> list[str]:
        return self._names("read")

    def input(self, op: str, name: str) -> Resource:
        """The last resource captured for ``(op, name)``."""
        for o, res in reversed(self.seen):
            if o == op and res.logical_name == name:
                return res
        raise KeyError(f"no {op} of {name!r} recorded")

    def seen_values(self, attr: str, *ops: str) -> list[Any]:
        """``getattr(res, attr)`` for each recorded call (optionally filtered to ``ops``)."""
        keep = set(ops) if ops else None
        return [getattr(res, attr, None) for o, res in self.seen if keep is None or o in keep]

    def created_ref(self, name: str) -> Any:
        return getattr(self.input("create", name), "ref", None)

    def deleted_output(self, name: str, key: str = "out") -> Any:
        return getattr(self.input("delete", name), key, None)

    # -- CRUD -------------------------------------------------------------

    def _record(self, op: str, res: Resource) -> None:
        self.calls.append((op, res.logical_name))
        self.seen.append((op, res))
        fail = {
            "create": self.fail_create,
            "update": self.fail_update,
            "delete": self.fail_delete,
            "read": self.fail_read,
        }[op]
        if res.logical_name in fail:
            raise RuntimeError(f"{op} failed for {res.logical_name}")

    def _emit(self, op: str, ctx: Context, res: Resource) -> dict[str, Any]:
        spec = self._on[op]
        if spec is None:
            return default_outputs(op, res)
        return spec(ctx, res) if callable(spec) else dict(spec)

    async def create(self, ctx: Context, res: Resource) -> dict[str, Any]:
        self._record("create", res)
        return self._emit("create", ctx, res)

    async def read(self, ctx: Context, res: Resource) -> dict[str, Any] | None:
        self._record("read", res)
        if self._live is not None:
            return self._live.get(res.logical_name)
        spec = self._on["read"]
        if spec is None:
            return None
        return spec(ctx, res) if callable(spec) else dict(spec)

    async def update(self, ctx: Context, prior: dict[str, Any], res: Resource) -> dict[str, Any]:
        self._record("update", res)
        return self._emit("update", ctx, res)

    async def delete(self, ctx: Context, res: Resource) -> None:
        self._record("delete", res)
