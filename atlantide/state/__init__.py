"""atlantide.state: the modular graph state store.

The engine talks only to :class:`StateBackend`; concrete backends (memory,
sqlite, s3, postgres) are interchangeable behind it and selected declaratively
via :func:`make_state_backend`. The remote backends are deliberately absent from
this namespace so their dependencies load only when configured.
"""

from atlantide.state.backend import Lease, StateBackend, StateGraph, StateNode
from atlantide.state.factory import StateConfig, make_state_backend
from atlantide.state.memory_backend import MemoryStateBackend
from atlantide.state.sqlite_backend import SqliteStateBackend

__all__ = [
    "Lease",
    "MemoryStateBackend",
    "SqliteStateBackend",
    "StateBackend",
    "StateConfig",
    "StateGraph",
    "StateNode",
    "make_state_backend",
]
