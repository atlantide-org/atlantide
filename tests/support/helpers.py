"""One-line helpers shared across suites.

Engine builders live in :mod:`tests.support.factories`; these are the small
readers and writers around them: a config file on disk, a changeset as a
``{node_id: Action}`` map, the DEBUG records one logger emits, and the leaf
errors of an ``ExceptionGroup``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from atlantide.reconcile import Action, ChangeSet


def write_config(directory: Path, source: str, name: str = "config.py") -> Path:
    """Write ``source`` to ``directory / name`` and return that path."""
    path = directory / name
    path.write_text(source, encoding="utf-8")
    return path


def actions_of(changeset: ChangeSet) -> dict[str, Action]:
    """``{node_id: action}`` for every change in ``changeset``, NOOPs included."""
    return {change.node_id: change.action for change in changeset}


@contextmanager
def debug_records(name: str) -> Iterator[list[logging.LogRecord]]:
    """Records ``name`` logs at DEBUG, whatever the ``atlantide`` root is configured to.

    The handler sits on ``name`` itself and the logger's own level is lowered for
    the duration, so neither the root's level nor propagation settings can hide a
    record. Both are restored on exit.
    """
    records: list[logging.LogRecord] = []
    handler = logging.Handler(logging.DEBUG)
    handler.emit = records.append  # type: ignore[method-assign]
    logger = logging.getLogger(name)
    level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(level)


def leaves(exc: BaseException) -> list[BaseException]:
    """The leaf errors of ``exc``, flattening nested exception groups depth-first.

    A plain exception is its own single leaf.
    """
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for inner in exc.exceptions for leaf in leaves(inner)]
    return [exc]
