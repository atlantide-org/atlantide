"""atlantide.state: the modular graph state store.

The engine talks only to :class:`StateBackend`; backends are selected by
:func:`make_state_backend`. The remote backends (s3, postgres) are not imported
here, so boto3 and psycopg load only when configured. See README.md.
"""

from atlantide.state.backend import StateBackend
from atlantide.state.factory import StateConfig, make_state_backend
from atlantide.state.leases import (
    DEFAULT_LOCK_POLICY,
    DEFAULT_SKEW_MARGIN,
    Lease,
    LeaseGuard,
    LockPolicy,
)
from atlantide.state.memory import MemoryStateBackend
from atlantide.state.model import (
    NO_INPUT_HASH,
    NodeStatus,
    StateGraph,
    StateNode,
)
from atlantide.state.sql.sqlite import SqliteStateBackend

__all__ = [
    "DEFAULT_LOCK_POLICY",
    "DEFAULT_SKEW_MARGIN",
    "NO_INPUT_HASH",
    "Lease",
    "LeaseGuard",
    "LockPolicy",
    "MemoryStateBackend",
    "NodeStatus",
    "SqliteStateBackend",
    "StateBackend",
    "StateConfig",
    "StateGraph",
    "StateNode",
    "make_state_backend",
]
