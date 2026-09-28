"""The table-shaped backends: sqlite (the local default) and postgres.

Both share the ``nodes`` / ``meta`` / ``locks`` schema and the row codec in
:mod:`atlantide.state.codec`. Nothing is imported here: ``sql.postgres`` needs the
optional ``psycopg`` driver, and ``sql.dsn`` is read by the CLI without it.
"""
