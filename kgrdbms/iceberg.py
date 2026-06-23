"""Apache Iceberg as a virtual-edge source.

A virtual edge normally resolves against an operational SQL store — Postgres or
SQLite, the live system of record. But a great deal of machine-generated
relationship data does not live there; it lands in a lakehouse: Iceberg tables in
object storage, versioned by snapshot and governed by a catalog. This module lets
a binding name a *catalog + table* and resolve edges straight out of Iceberg, with
DuckDB as the query engine — so the graph reads the lake live, copies nothing, and
honors the same parameterized-query contract as every other virtual edge.

The division of labor mirrors Iceberg's own architecture:

  * **pyiceberg owns identity and versioning.** It loads the catalog, maps a
    ``namespace.table`` name to the current metadata pointer, and — when a
    ``snapshot_id`` is bound — pins a specific version for time-travel reads. This
    is precisely the layer a schema graph cares about: the stable name and the
    snapshot, not the bytes.

  * **DuckDB owns the scan.** Given the resolved metadata location, DuckDB's
    ``iceberg`` extension reads the table (format-version 2 today; newer format
    versions transparently as the extension gains them) and answers the binding's
    SQL. The table is exposed as a named DuckDB VIEW, so the binding's query is
    written against an ordinary table name — byte-for-byte identical to the
    Postgres/SQLite case, which is why the resolver's bind path is untouched.

Credentials never live in the graph. Any catalog property may be written as
``env:VAR_NAME`` and is resolved from the environment at query time — the same
discipline as ``dsn_env`` on a SQL virtual edge.
"""

from __future__ import annotations

from typing import Any

# DuckDB's positional placeholder — identical to sqlite's, so resolve()'s
# bind-by-marker logic needs no special case once we hand it this connection.
MARKER = "?"

_ENV_PREFIX = "env:"


def _resolve_env(value: Any) -> Any:
    """A catalog property of the form ``env:VAR`` becomes the variable's value.

    Keeps secrets (catalog tokens, S3 keys, signer URIs) out of the stored
    binding: the graph holds the *name* of the env var, never the secret. Any
    non-string or non-prefixed value passes through unchanged.
    """
    if isinstance(value, str) and value.startswith(_ENV_PREFIX):
        import os

        name = value[len(_ENV_PREFIX) :]
        resolved = os.environ.get(name)
        if not resolved:
            raise RuntimeError(
                f"iceberg catalog property references env var {name!r}, which is "
                f"unset — cannot reach the catalog."
            )
        return resolved
    return value


def resolve_catalog_props(catalog: dict[str, Any]) -> dict[str, Any]:
    """Catalog config with every ``env:VAR`` value substituted from the env.

    The catalog dict is pyiceberg's own property bag (``uri``, ``warehouse``,
    ``type``, ``token``, S3 creds, …) minus the reserved ``name`` key, which
    names the catalog rather than configuring it.
    """
    return {
        k: _resolve_env(v) for k, v in catalog.items() if k != "name"
    }


def _load_table(catalog: dict[str, Any], table: str):
    """Load a pyiceberg table from a catalog config + ``namespace.table`` name."""
    try:
        from pyiceberg.catalog import load_catalog  # type: ignore
    except ModuleNotFoundError as e:  # pragma: no cover - env dependent
        raise RuntimeError(
            "iceberg virtual edge needs the iceberg extra: pip install "
            "'knowledge-graph-rdbms[iceberg]'"
        ) from e
    name = catalog.get("name", "default")
    return load_catalog(name, **resolve_catalog_props(catalog)).load_table(table)


def _view_name(table: str) -> str:
    """The DuckDB view name a binding's query references — the table's leaf name.

    ``analytics.co_held`` → ``co_held``. The binding author writes plain
    ``FROM co_held``, exactly as they would against a Postgres relation.
    """
    return table.rsplit(".", 1)[-1]


def open_source(
    catalog: dict[str, Any], table: str, snapshot_id: int | None = None
) -> tuple[Any, str]:
    """Open an Iceberg table as a queryable DuckDB connection.

    Resolves ``table`` against ``catalog`` via pyiceberg (pinning ``snapshot_id``
    if given), loads DuckDB's iceberg extension, and registers the table as a VIEW
    named after its leaf segment. Returns ``(connection, marker)`` shaped exactly
    like :func:`kgrdbms.virtual._connect`, so :func:`kgrdbms.virtual.resolve` runs
    the binding's parameterized query against it with no special-casing.
    """
    try:
        import duckdb  # type: ignore
    except ModuleNotFoundError as e:  # pragma: no cover - env dependent
        raise RuntimeError(
            "iceberg virtual edge needs the iceberg extra: pip install "
            "'knowledge-graph-rdbms[iceberg]'"
        ) from e

    tbl = _load_table(catalog, table)
    metadata_location = tbl.metadata_location

    conn = duckdb.connect()
    # INSTALL is idempotent and cached; LOAD makes iceberg_scan available. Kept
    # here (not at import) so importing this module never reaches the network.
    conn.execute("INSTALL iceberg")
    conn.execute("LOAD iceberg")

    view = _view_name(table)
    # The metadata location is operator-resolved (from the catalog), not user
    # input, so formatting it into the DDL is safe — and DuckDB's scan function
    # takes a path literal, not a bound parameter, for the table source.
    scan = f"iceberg_scan('{metadata_location}'"
    if snapshot_id is not None:
        scan += f", snapshot_from_id => {int(snapshot_id)}"
    scan += ")"
    conn.execute(f'CREATE VIEW "{view}" AS SELECT * FROM {scan}')
    return conn, MARKER
