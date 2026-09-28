"""Postgres connection strings, read without the driver.

A DSN routinely carries a password, and anything derived from one reaches
terminals, CI logs and audit records. These helpers find the password to scrub
it, and reduce a DSN to a printable ``host[:port]``. No ``psycopg`` import: the
CLI uses them on machines without the ``postgres`` extra.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlsplit

__all__ = ["REDACTED", "dsn_host", "dsn_password", "scrub_dsn"]

#: ``password=...`` in a key/value conninfo string: quoted (with ``\'`` escapes)
#: or bare up to the next whitespace.
_KV_PASSWORD_RE = re.compile(r"""\bpassword\s*=\s*('(?:[^'\\]|\\.)*'|\S+)""")

REDACTED = "***"


def dsn_password(dsn: str) -> str | None:
    """The password ``dsn`` carries, parsed without the driver, if one is there.

    Both libpq forms: a URL (userinfo, or a ``password`` query parameter) and a
    ``key=value`` string.
    """
    if "://" in dsn:
        try:
            parts = urlsplit(dsn)
            password = parts.password or parse_qs(parts.query).get("password", [None])[0]
        except ValueError:
            password = None
        if password:
            return password
    match = _KV_PASSWORD_RE.search(dsn)
    if match:
        value = match.group(1)
        return value[1:-1] if value.startswith("'") and value.endswith("'") else value
    return None


def scrub_dsn(text: str, dsn: str) -> str:
    """``text`` with ``dsn`` and its password taken out.

    Driver messages can quote the connection string they were handed, password
    included (see the module doc).
    """
    if dsn:
        text = text.replace(dsn, "<dsn>")
    password = dsn_password(dsn)
    if password:
        for form in {password, unquote(password)}:
            text = text.replace(form, REDACTED)
    return text


def dsn_host(dsn: str | None) -> str:
    """The ``host[:port]`` of a DSN, with any credentials dropped."""
    if not dsn:
        return "?"
    try:
        parsed = urlsplit(dsn)
    except ValueError:  # pragma: no cover - defensive
        return "?"
    host = parsed.hostname or "?"
    return f"{host}:{parsed.port}" if parsed.port is not None else host
