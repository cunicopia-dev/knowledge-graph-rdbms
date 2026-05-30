"""Postgres engine — live.

The scale-up of the SQLite default without changing engines conceptually. Same
five-table relational model and the same query shapes (point lookups, a
`WITH RECURSIVE` traversal, BFS over `out`/`in_`), ported to Postgres: `%s`
placeholders, `ON CONFLICT` upserts, `jsonb` properties, `= ANY(%s)` array
membership instead of SQLite's chunked `IN (...)`. The win over SQLite is real
concurrent writers and a server you can scale, while keeping SQL you can read.

`location` is a libpq connection string / DSN (e.g.
`postgresql://user:pass@host:5432/db`), not a file path. The event log does NOT
live here — for a non-sqlite backend it lives in a control-plane SQLite store
(see `resolver`), so this class is purely the graph *projection*.

`psycopg` (v3) is imported lazily so `import kgrdbms.backends` works without it;
a missing driver raises `NotImplementedError` (routed to "unavailable" by the
front doors) telling you to install the `postgres` extra.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Any, Iterable, Iterator

from kgrdbms.backends import backend
from kgrdbms.backends.base import GraphBackend
from kgrdbms.graph import Edge, Node


_SCHEMA_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS nodes (
        id          TEXT PRIMARY KEY,
        kind        TEXT NOT NULL,
        name        TEXT NOT NULL,
        created_at  TIMESTAMPTZ DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS node_labels (
        node_id  TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
        label    TEXT NOT NULL,
        PRIMARY KEY (node_id, label)
    )""",
    """CREATE TABLE IF NOT EXISTS node_properties (
        node_id     TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
        key         TEXT NOT NULL,
        value_json  JSONB NOT NULL,
        PRIMARY KEY (node_id, key)
    )""",
    """CREATE TABLE IF NOT EXISTS edges (
        id          TEXT PRIMARY KEY,
        from_node   TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
        to_node     TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
        type        TEXT NOT NULL,
        created_at  TIMESTAMPTZ DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS edge_properties (
        edge_id     TEXT NOT NULL REFERENCES edges(id) ON DELETE CASCADE,
        key         TEXT NOT NULL,
        value_json  JSONB NOT NULL,
        PRIMARY KEY (edge_id, key)
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_edges_triple ON edges(from_node, type, to_node)",
    "CREATE INDEX IF NOT EXISTS idx_nodes_kind        ON nodes(kind)",
    "CREATE INDEX IF NOT EXISTS idx_node_labels_label ON node_labels(label)",
    "CREATE INDEX IF NOT EXISTS idx_edges_from_type   ON edges(from_node, type)",
    "CREATE INDEX IF NOT EXISTS idx_edges_to_type     ON edges(to_node, type)",
    "CREATE INDEX IF NOT EXISTS idx_edges_type        ON edges(type)",
]


class PostgresGraph:
    """A label property graph over Postgres. Satisfies the `GraphBackend` surface."""

    def __init__(self, location: str, **options: Any) -> None:
        try:
            import psycopg
            from psycopg.rows import dict_row
            from psycopg.types.json import Jsonb
        except ImportError as e:  # pragma: no cover - exercised only without the extra
            raise NotImplementedError(
                "postgres backend needs psycopg: pip install 'knowledge-graph-rdbms[postgres]'"
            ) from e
        self._Jsonb = Jsonb
        self.location = location
        self.path = location  # callers may read .path; for pg it's the DSN
        self.conn = psycopg.connect(location, row_factory=dict_row)
        self._batch_depth = 0
        for stmt in _SCHEMA_STATEMENTS:
            self.conn.execute(stmt)
        self.conn.commit()

    # ---- lifecycle / transactions (mirror Graph's batch semantics) -----

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def _maybe_commit(self) -> None:
        if self._batch_depth == 0:
            self.conn.commit()

    @contextmanager
    def tx(self) -> Iterator[Any]:
        if self._batch_depth:
            yield self.conn
            return
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    @contextmanager
    def batch(self) -> Iterator["PostgresGraph"]:
        self._batch_depth += 1
        try:
            yield self
        except Exception:
            self._batch_depth -= 1
            if self._batch_depth == 0:
                self.conn.rollback()
            raise
        else:
            self._batch_depth -= 1
            if self._batch_depth == 0:
                self.conn.commit()

    def clear(self) -> None:
        with self.tx():
            self.conn.execute("TRUNCATE nodes, node_labels, node_properties, edges, edge_properties")

    # ---- writes --------------------------------------------------------

    def add_node(
        self,
        id: str,
        kind: str,
        name: str | None = None,
        labels: Iterable[str] = (),
        properties: dict[str, Any] | None = None,
    ) -> Node:
        name = name or id
        properties = dict(properties or {})
        with self.tx():
            self.conn.execute(
                "INSERT INTO nodes(id, kind, name) VALUES (%s, %s, %s) "
                "ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, name=excluded.name",
                (id, kind, name),
            )
            for label in set(labels):
                self.conn.execute(
                    "INSERT INTO node_labels(node_id, label) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (id, label),
                )
            for k, v in properties.items():
                self.conn.execute(
                    "INSERT INTO node_properties(node_id, key, value_json) VALUES (%s, %s, %s) "
                    "ON CONFLICT(node_id, key) DO UPDATE SET value_json=excluded.value_json",
                    (id, k, self._Jsonb(v)),
                )
        return Node(id=id, kind=kind, name=name, labels=set(labels), properties=properties)

    def add_label(self, node_id: str, *labels: str) -> None:
        with self.tx():
            for label in labels:
                self.conn.execute(
                    "INSERT INTO node_labels(node_id, label) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (node_id, label),
                )

    def remove_label(self, node_id: str, label: str) -> None:
        with self.tx():
            self.conn.execute(
                "DELETE FROM node_labels WHERE node_id=%s AND label=%s", (node_id, label)
            )

    def set_property(self, node_id: str, key: str, value: Any) -> None:
        with self.tx():
            self.conn.execute(
                "INSERT INTO node_properties(node_id, key, value_json) VALUES (%s, %s, %s) "
                "ON CONFLICT(node_id, key) DO UPDATE SET value_json=excluded.value_json",
                (node_id, key, self._Jsonb(value)),
            )

    def del_property(self, node_id: str, key: str) -> None:
        with self.tx():
            self.conn.execute(
                "DELETE FROM node_properties WHERE node_id=%s AND key=%s", (node_id, key)
            )

    def delete_node(self, node_id: str) -> bool:
        existed = self.node(node_id) is not None
        with self.tx():
            self.conn.execute("DELETE FROM nodes WHERE id=%s", (node_id,))
        return existed

    def add_edge(
        self,
        from_node: str,
        to_node: str,
        type: str,
        properties: dict[str, Any] | None = None,
    ) -> Edge:
        with self.tx():
            row = self.conn.execute(
                "SELECT id FROM edges WHERE from_node=%s AND type=%s AND to_node=%s",
                (from_node, type, to_node),
            ).fetchone()
            if row:
                edge_id = row["id"]
            else:
                edge_id = uuid.uuid4().hex
                self.conn.execute(
                    "INSERT INTO edges(id, from_node, to_node, type) VALUES (%s, %s, %s, %s)",
                    (edge_id, from_node, to_node, type),
                )
            props = dict(properties or {})
            for k, v in props.items():
                self.conn.execute(
                    "INSERT INTO edge_properties(edge_id, key, value_json) VALUES (%s, %s, %s) "
                    "ON CONFLICT(edge_id, key) DO UPDATE SET value_json=excluded.value_json",
                    (edge_id, k, self._Jsonb(v)),
                )
        return Edge(id=edge_id, from_node=from_node, to_node=to_node, type=type, properties=props)

    def delete_edge(self, from_node: str, to_node: str, type: str) -> int:
        with self.tx():
            cur = self.conn.execute(
                "DELETE FROM edges WHERE from_node=%s AND type=%s AND to_node=%s",
                (from_node, type, to_node),
            )
            return cur.rowcount

    def incident_edges(self, node_id: str) -> list[Edge]:
        rows = self.conn.execute(
            "SELECT * FROM edges WHERE from_node=%s OR to_node=%s", (node_id, node_id)
        ).fetchall()
        return [self._hydrate_edge(r) for r in rows]

    # ---- reads ---------------------------------------------------------

    def node(self, id: str) -> Node | None:
        row = self.conn.execute("SELECT * FROM nodes WHERE id=%s", (id,)).fetchone()
        return self._hydrate_node(row) if row else None

    def nodes_by_kind(self, kind: str) -> list[Node]:
        rows = self.conn.execute(
            "SELECT * FROM nodes WHERE kind=%s ORDER BY id", (kind,)
        ).fetchall()
        return self._hydrate_nodes(rows)

    def nodes_by_label(self, label: str) -> list[Node]:
        rows = self.conn.execute(
            "SELECT n.* FROM nodes n JOIN node_labels l ON l.node_id = n.id "
            "WHERE l.label = %s ORDER BY n.id",
            (label,),
        ).fetchall()
        return self._hydrate_nodes(rows)

    def out(self, node_id: str, edge_type: str | None = None) -> list[tuple[Edge, Node]]:
        base = (
            "SELECT e.*, n.id AS n_id, n.kind AS n_kind, n.name AS n_name "
            "FROM edges e JOIN nodes n ON n.id = e.to_node WHERE e.from_node = %s"
        )
        if edge_type:
            rows = self.conn.execute(base + " AND e.type = %s ORDER BY e.type, n.id",
                                     (node_id, edge_type)).fetchall()
        else:
            rows = self.conn.execute(base + " ORDER BY e.type, n.id", (node_id,)).fetchall()
        return self._rows_to_edge_node_pairs(rows)

    def in_(self, node_id: str, edge_type: str | None = None) -> list[tuple[Edge, Node]]:
        base = (
            "SELECT e.*, n.id AS n_id, n.kind AS n_kind, n.name AS n_name "
            "FROM edges e JOIN nodes n ON n.id = e.from_node WHERE e.to_node = %s"
        )
        if edge_type:
            rows = self.conn.execute(base + " AND e.type = %s ORDER BY e.type, n.id",
                                     (node_id, edge_type)).fetchall()
        else:
            rows = self.conn.execute(base + " ORDER BY e.type, n.id", (node_id,)).fetchall()
        return self._rows_to_edge_node_pairs(rows)

    def descendants(self, node_id: str, edge_type: str, max_depth: int = 6) -> list[Node]:
        rows = self.conn.execute(
            """
            WITH RECURSIVE walk(depth, node_id) AS (
                SELECT 0, %s
                UNION ALL
                SELECT walk.depth + 1, e.to_node
                FROM walk JOIN edges e ON e.from_node = walk.node_id
                WHERE e.type = %s AND walk.depth < %s
            )
            SELECT DISTINCT n.* FROM walk JOIN nodes n ON n.id = walk.node_id
            WHERE walk.depth > 0
            """,
            (node_id, edge_type, max_depth),
        ).fetchall()
        return self._hydrate_nodes(rows)

    # neighborhood + shortest_path are pure BFS over out/in_/node — identical to
    # the SQLite engine, since they touch no SQL directly.

    def neighborhood(self, node_id: str, depth: int = 1) -> dict[str, Node]:
        depth = max(0, depth)
        if self.conn.execute("SELECT 1 FROM nodes WHERE id=%s", (node_id,)).fetchone() is None:
            return {}
        seen_ids: set[str] = {node_id}
        frontier = {node_id}
        for _ in range(depth):
            nxt: set[str] = set()
            for nid in frontier:
                for _, nb in self.out(nid):
                    if nb.id not in seen_ids:
                        seen_ids.add(nb.id)
                        nxt.add(nb.id)
                for _, nb in self.in_(nid):
                    if nb.id not in seen_ids:
                        seen_ids.add(nb.id)
                        nxt.add(nb.id)
            frontier = nxt
            if not frontier:
                break
        rows = self.conn.execute(
            "SELECT * FROM nodes WHERE id = ANY(%s)", (list(seen_ids),)
        ).fetchall()
        return {n.id: n for n in self._hydrate_nodes(rows)}

    def shortest_path(self, from_id: str, to_id: str, max_depth: int = 8) -> list[Node] | None:
        if from_id == to_id:
            n = self.node(from_id)
            return [n] if n else None
        visited: dict[str, str | None] = {from_id: None}
        frontier = [from_id]
        for _ in range(max_depth):
            nxt: list[str] = []
            for nid in frontier:
                for _, nb in self.out(nid):
                    if nb.id in visited:
                        continue
                    visited[nb.id] = nid
                    if nb.id == to_id:
                        return self._reconstruct_path(visited, to_id)
                    nxt.append(nb.id)
                for _, nb in self.in_(nid):
                    if nb.id in visited:
                        continue
                    visited[nb.id] = nid
                    if nb.id == to_id:
                        return self._reconstruct_path(visited, to_id)
                    nxt.append(nb.id)
            frontier = nxt
            if not frontier:
                break
        return None

    def _reconstruct_path(self, parents: dict[str, str | None], target: str) -> list[Node]:
        seq: list[str] = []
        cur: str | None = target
        while cur is not None:
            seq.append(cur)
            cur = parents.get(cur)
        seq.reverse()
        return [n for n in (self.node(nid) for nid in seq) if n is not None]

    # ---- stats ---------------------------------------------------------

    def count_nodes_by_kind(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT kind, COUNT(*) AS c FROM nodes GROUP BY kind ORDER BY c DESC"
        ).fetchall()
        return {r["kind"]: r["c"] for r in rows}

    def count_edges_by_type(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT type, COUNT(*) AS c FROM edges GROUP BY type ORDER BY c DESC"
        ).fetchall()
        return {r["type"]: r["c"] for r in rows}

    def total_nodes(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS c FROM nodes").fetchone()["c"]

    def total_edges(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS c FROM edges").fetchone()["c"]

    # ---- hydration (jsonb returns parsed values — no json.loads) --------

    def _hydrate_node(self, row: dict) -> Node:
        nid = row["id"]
        labels = {
            r["label"]
            for r in self.conn.execute(
                "SELECT label FROM node_labels WHERE node_id=%s", (nid,)
            ).fetchall()
        }
        props = {
            r["key"]: r["value_json"]
            for r in self.conn.execute(
                "SELECT key, value_json FROM node_properties WHERE node_id=%s", (nid,)
            ).fetchall()
        }
        return Node(id=nid, kind=row["kind"], name=row["name"], labels=labels, properties=props)

    def _hydrate_nodes(self, rows: Iterable[dict]) -> list[Node]:
        rows = list(rows)
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        labels_by: dict[str, set[str]] = {nid: set() for nid in ids}
        props_by: dict[str, dict[str, Any]] = {nid: {} for nid in ids}
        for r in self.conn.execute(
            "SELECT node_id, label FROM node_labels WHERE node_id = ANY(%s)", (ids,)
        ).fetchall():
            labels_by[r["node_id"]].add(r["label"])
        for r in self.conn.execute(
            "SELECT node_id, key, value_json FROM node_properties WHERE node_id = ANY(%s)", (ids,)
        ).fetchall():
            props_by[r["node_id"]][r["key"]] = r["value_json"]
        return [
            Node(id=r["id"], kind=r["kind"], name=r["name"],
                 labels=labels_by[r["id"]], properties=props_by[r["id"]])
            for r in rows
        ]

    def _rows_to_edge_node_pairs(self, rows: Iterable[dict]) -> list[tuple[Edge, Node]]:
        rows = list(rows)
        if not rows:
            return []
        eids = [r["id"] for r in rows]
        props_by: dict[str, dict[str, Any]] = {eid: {} for eid in eids}
        for pr in self.conn.execute(
            "SELECT edge_id, key, value_json FROM edge_properties WHERE edge_id = ANY(%s)", (eids,)
        ).fetchall():
            props_by[pr["edge_id"]][pr["key"]] = pr["value_json"]
        return [
            (
                Edge(id=r["id"], from_node=r["from_node"], to_node=r["to_node"],
                     type=r["type"], properties=props_by[r["id"]]),
                Node(id=r["n_id"], kind=r["n_kind"], name=r["n_name"]),
            )
            for r in rows
        ]

    def _hydrate_edge(self, row: dict) -> Edge:
        eid = row["id"]
        props = {
            r["key"]: r["value_json"]
            for r in self.conn.execute(
                "SELECT key, value_json FROM edge_properties WHERE edge_id=%s", (eid,)
            ).fetchall()
        }
        return Edge(id=eid, from_node=row["from_node"], to_node=row["to_node"],
                    type=row["type"], properties=props)


@backend("postgres")
def open_postgres(*, location: str, **options: Any) -> GraphBackend:
    return PostgresGraph(location, **options)
