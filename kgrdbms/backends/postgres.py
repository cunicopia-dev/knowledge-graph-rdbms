"""Postgres engine — STUB.

Strategic role: the *scale-up* of the SQLite default without changing engines
conceptually. Postgres speaks the same relational model and supports recursive
CTEs, so the query shapes in `graph.py` (point lookups, `WITH RECURSIVE`
traversals, the five-table schema) port almost verbatim — swap `sqlite3` for a
driver, `?` for `%s`, and JSON1 for `jsonb`. The win over SQLite: real
concurrent writers and a server you can scale, while keeping SQL you can read.

Implementation sketch (when we build it):
  * `location` is a DSN/connection string, not a file path.
  * Mirror `graph.py`'s SQL; `jsonb` for properties (round-trips richer than
    SQLite's TEXT-encoded JSON).
  * The event log can co-locate (a `graph_events` table in the same database)
    OR live in control-plane SQLite — decide per the audit-ownership question.

`location` is stored but no connection is opened; every method raises via
`_StubBackend` with exactly what's missing. The surface to fill in is the
`GraphBackend` Protocol — nothing more.
"""

from __future__ import annotations

from typing import Any

from kgrdbms.backends import backend
from kgrdbms.backends.base import GraphBackend, _StubBackend


class PostgresGraph(_StubBackend):
    engine = "postgres"
    # TODO: open a connection pool from the DSN in `self.location`, apply the
    # schema, and override _StubBackend's methods with jsonb-backed SQL.


@backend("postgres")
def open_postgres(*, location: str, **options: Any) -> GraphBackend:
    return PostgresGraph(location, **options)
