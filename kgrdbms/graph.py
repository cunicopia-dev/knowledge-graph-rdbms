"""A label property graph on top of an RDBMS (SQLite).

No graph database, no Cypher, no external server. Five tables, a handful of
indexes, and a small Python API. The schema is intentionally minimal.

A label property graph only needs four primitives:

    Node       — has a stable id, a kind, a display name.
    Edge       — typed, directed, from one node to another.
    Label      — a node's set memberships (many labels per node).
    Property   — key/value bag on a node or an edge (values stored as JSON).

Everything else is indexes and query ergonomics.

Storage location:
    Defaults to ~/.kgrdbms/graph.db. Override with the KGRDBMS_HOME
    environment variable, or pass `path=` explicitly.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator


# ---- storage ---------------------------------------------------------


def default_graph_path() -> Path:
    base = os.environ.get("KGRDBMS_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".kgrdbms"
    root.mkdir(parents=True, exist_ok=True)
    return root / "graph.db"


# ---- schema ----------------------------------------------------------


SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS node_labels (
    node_id  TEXT NOT NULL,
    label    TEXT NOT NULL,
    PRIMARY KEY (node_id, label),
    FOREIGN KEY (node_id) REFERENCES nodes(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS node_properties (
    node_id     TEXT NOT NULL,
    key         TEXT NOT NULL,
    value_json  TEXT NOT NULL,
    PRIMARY KEY (node_id, key),
    FOREIGN KEY (node_id) REFERENCES nodes(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS edges (
    id          TEXT PRIMARY KEY,
    from_node   TEXT NOT NULL,
    to_node     TEXT NOT NULL,
    type        TEXT NOT NULL,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (from_node) REFERENCES nodes(id) ON DELETE CASCADE,
    FOREIGN KEY (to_node)   REFERENCES nodes(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS edge_properties (
    edge_id     TEXT NOT NULL,
    key         TEXT NOT NULL,
    value_json  TEXT NOT NULL,
    PRIMARY KEY (edge_id, key),
    FOREIGN KEY (edge_id) REFERENCES edges(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_edges_triple
    ON edges(from_node, type, to_node);

CREATE INDEX IF NOT EXISTS idx_nodes_kind         ON nodes(kind);
CREATE INDEX IF NOT EXISTS idx_node_labels_label  ON node_labels(label);
CREATE INDEX IF NOT EXISTS idx_edges_from_type    ON edges(from_node, type);
CREATE INDEX IF NOT EXISTS idx_edges_to_type      ON edges(to_node, type);
CREATE INDEX IF NOT EXISTS idx_edges_type         ON edges(type);
"""


# ---- dataclasses -----------------------------------------------------


@dataclass
class Node:
    id: str
    kind: str
    name: str
    labels: set[str] = field(default_factory=set)
    properties: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"Node({self.kind}:{self.id} {self.name!r})"


@dataclass
class Edge:
    id: str
    from_node: str
    to_node: str
    type: str
    properties: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"Edge({self.from_node} -[{self.type}]-> {self.to_node})"


# ---- slugs -----------------------------------------------------------


def slug(text: str, *, prefix: str | None = None) -> str:
    """Stable slug for natural-language concept names.

    Deduplication happens here: any two strings that slugify the same become
    the same node. This is the load-bearing trick for natural-language ids.
    """
    s = text.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-") or "anon"
    return f"{prefix}:{s}" if prefix else s


# ---- graph ----------------------------------------------------------


class Graph:
    """A label property graph backed by SQLite.

    Use as a context manager or remember to call .close().
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else default_graph_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- lifecycle -------------------------------------------------

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def __enter__(self) -> "Graph":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Explicit transaction wrapper. Commits on success, rolls back on raise."""
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def clear(self) -> None:
        """Wipe the entire graph."""
        with self.tx():
            for tbl in ("edge_properties", "edges", "node_properties", "node_labels", "nodes"):
                self.conn.execute(f"DELETE FROM {tbl}")

    # ---- writes ----------------------------------------------------

    def add_node(
        self,
        id: str,
        kind: str,
        name: str | None = None,
        labels: Iterable[str] = (),
        properties: dict[str, Any] | None = None,
    ) -> Node:
        """Insert or update a node. Idempotent on id."""
        name = name or id
        properties = dict(properties or {})
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO nodes(id, kind, name) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, name=excluded.name",
            (id, kind, name),
        )
        for label in set(labels):
            cur.execute(
                "INSERT OR IGNORE INTO node_labels(node_id, label) VALUES (?, ?)",
                (id, label),
            )
        for k, v in properties.items():
            cur.execute(
                "INSERT INTO node_properties(node_id, key, value_json) VALUES (?, ?, ?) "
                "ON CONFLICT(node_id, key) DO UPDATE SET value_json=excluded.value_json",
                (id, k, json.dumps(v)),
            )
        self.conn.commit()
        return Node(id=id, kind=kind, name=name, labels=set(labels), properties=properties)

    def add_label(self, node_id: str, *labels: str) -> None:
        with self.tx():
            for label in labels:
                self.conn.execute(
                    "INSERT OR IGNORE INTO node_labels(node_id, label) VALUES (?, ?)",
                    (node_id, label),
                )

    def set_property(self, node_id: str, key: str, value: Any) -> None:
        with self.tx():
            self.conn.execute(
                "INSERT INTO node_properties(node_id, key, value_json) VALUES (?, ?, ?) "
                "ON CONFLICT(node_id, key) DO UPDATE SET value_json=excluded.value_json",
                (node_id, key, json.dumps(value)),
            )

    def add_edge(
        self,
        from_node: str,
        to_node: str,
        type: str,
        properties: dict[str, Any] | None = None,
    ) -> Edge:
        """Add an edge. Triples (from, type, to) are unique — re-adding updates properties."""
        row = self.conn.execute(
            "SELECT id FROM edges WHERE from_node=? AND type=? AND to_node=?",
            (from_node, type, to_node),
        ).fetchone()
        if row:
            edge_id = row["id"]
        else:
            edge_id = uuid.uuid4().hex
            self.conn.execute(
                "INSERT INTO edges(id, from_node, to_node, type) VALUES (?, ?, ?, ?)",
                (edge_id, from_node, to_node, type),
            )
        props = dict(properties or {})
        for k, v in props.items():
            self.conn.execute(
                "INSERT INTO edge_properties(edge_id, key, value_json) VALUES (?, ?, ?) "
                "ON CONFLICT(edge_id, key) DO UPDATE SET value_json=excluded.value_json",
                (edge_id, k, json.dumps(v)),
            )
        self.conn.commit()
        return Edge(id=edge_id, from_node=from_node, to_node=to_node, type=type, properties=props)

    # ---- deletes / un-sets (single path; events + compensation reuse these) --

    def remove_label(self, node_id: str, label: str) -> None:
        with self.tx():
            self.conn.execute(
                "DELETE FROM node_labels WHERE node_id=? AND label=?", (node_id, label)
            )

    def del_property(self, node_id: str, key: str) -> None:
        with self.tx():
            self.conn.execute(
                "DELETE FROM node_properties WHERE node_id=? AND key=?", (node_id, key)
            )

    def delete_node(self, node_id: str) -> bool:
        """Delete a node. Foreign-key cascade removes its labels, properties, and edges."""
        existed = self.node(node_id) is not None
        with self.tx():
            self.conn.execute("DELETE FROM nodes WHERE id=?", (node_id,))
        return existed

    def delete_edge(self, from_node: str, to_node: str, type: str) -> int:
        """Delete a specific edge by triple. Returns rows removed (0 or 1)."""
        with self.tx():
            cur = self.conn.execute(
                "DELETE FROM edges WHERE from_node=? AND type=? AND to_node=?",
                (from_node, type, to_node),
            )
            return cur.rowcount

    def incident_edges(self, node_id: str) -> list[Edge]:
        """All edges touching a node (in or out). Used to capture state before a delete."""
        rows = self.conn.execute(
            "SELECT * FROM edges WHERE from_node=? OR to_node=?", (node_id, node_id)
        ).fetchall()
        return [self._hydrate_edge(r) for r in rows]

    # ---- reads -----------------------------------------------------

    def node(self, id: str) -> Node | None:
        row = self.conn.execute("SELECT * FROM nodes WHERE id=?", (id,)).fetchone()
        if not row:
            return None
        return self._hydrate_node(row)

    def nodes_by_kind(self, kind: str) -> list[Node]:
        rows = self.conn.execute("SELECT * FROM nodes WHERE kind=? ORDER BY id", (kind,)).fetchall()
        return [self._hydrate_node(r) for r in rows]

    def nodes_by_label(self, label: str) -> list[Node]:
        rows = self.conn.execute(
            "SELECT n.* FROM nodes n JOIN node_labels l ON l.node_id = n.id "
            "WHERE l.label = ? ORDER BY n.id",
            (label,),
        ).fetchall()
        return [self._hydrate_node(r) for r in rows]

    def out(self, node_id: str, edge_type: str | None = None) -> list[tuple[Edge, Node]]:
        """Outbound edges and their target nodes."""
        if edge_type:
            rows = self.conn.execute(
                "SELECT e.*, n.id AS n_id, n.kind AS n_kind, n.name AS n_name "
                "FROM edges e JOIN nodes n ON n.id = e.to_node "
                "WHERE e.from_node = ? AND e.type = ? ORDER BY e.type, n.id",
                (node_id, edge_type),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT e.*, n.id AS n_id, n.kind AS n_kind, n.name AS n_name "
                "FROM edges e JOIN nodes n ON n.id = e.to_node "
                "WHERE e.from_node = ? ORDER BY e.type, n.id",
                (node_id,),
            ).fetchall()
        return [(self._hydrate_edge(r), self._hydrate_node_lite(r)) for r in rows]

    def in_(self, node_id: str, edge_type: str | None = None) -> list[tuple[Edge, Node]]:
        """Inbound edges and their source nodes."""
        if edge_type:
            rows = self.conn.execute(
                "SELECT e.*, n.id AS n_id, n.kind AS n_kind, n.name AS n_name "
                "FROM edges e JOIN nodes n ON n.id = e.from_node "
                "WHERE e.to_node = ? AND e.type = ? ORDER BY e.type, n.id",
                (node_id, edge_type),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT e.*, n.id AS n_id, n.kind AS n_kind, n.name AS n_name "
                "FROM edges e JOIN nodes n ON n.id = e.from_node "
                "WHERE e.to_node = ? ORDER BY e.type, n.id",
                (node_id,),
            ).fetchall()
        return [(self._hydrate_edge(r), self._hydrate_node_lite(r)) for r in rows]

    def neighborhood(self, node_id: str, depth: int = 1) -> dict[str, Node]:
        """All nodes reachable from node_id within `depth` undirected hops (inclusive of self)."""
        depth = max(0, depth)
        seen: dict[str, Node] = {}
        root = self.node(node_id)
        if not root:
            return seen
        seen[node_id] = root
        frontier = {node_id}
        for _ in range(depth):
            next_frontier: set[str] = set()
            for nid in frontier:
                for _, neighbor in self.out(nid):
                    if neighbor.id not in seen:
                        seen[neighbor.id] = self.node(neighbor.id) or neighbor
                        next_frontier.add(neighbor.id)
                for _, neighbor in self.in_(nid):
                    if neighbor.id not in seen:
                        seen[neighbor.id] = self.node(neighbor.id) or neighbor
                        next_frontier.add(neighbor.id)
            frontier = next_frontier
            if not frontier:
                break
        return seen

    def shortest_path(self, from_id: str, to_id: str, max_depth: int = 8) -> list[Node] | None:
        """Undirected BFS. Returns the node sequence, or None if no path."""
        if from_id == to_id:
            n = self.node(from_id)
            return [n] if n else None
        visited: dict[str, str | None] = {from_id: None}  # child -> parent
        frontier = [from_id]
        for _ in range(max_depth):
            next_frontier: list[str] = []
            for nid in frontier:
                for _, neighbor in self.out(nid):
                    if neighbor.id in visited:
                        continue
                    visited[neighbor.id] = nid
                    if neighbor.id == to_id:
                        return self._reconstruct_path(visited, to_id)
                    next_frontier.append(neighbor.id)
                for _, neighbor in self.in_(nid):
                    if neighbor.id in visited:
                        continue
                    visited[neighbor.id] = nid
                    if neighbor.id == to_id:
                        return self._reconstruct_path(visited, to_id)
                    next_frontier.append(neighbor.id)
            frontier = next_frontier
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

    # ---- stats -----------------------------------------------------

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

    # ---- hydration -------------------------------------------------

    def _hydrate_node(self, row: sqlite3.Row) -> Node:
        nid = row["id"]
        labels = {
            r["label"]
            for r in self.conn.execute(
                "SELECT label FROM node_labels WHERE node_id=?", (nid,)
            ).fetchall()
        }
        props = {
            r["key"]: json.loads(r["value_json"])
            for r in self.conn.execute(
                "SELECT key, value_json FROM node_properties WHERE node_id=?", (nid,)
            ).fetchall()
        }
        return Node(id=nid, kind=row["kind"], name=row["name"], labels=labels, properties=props)

    @staticmethod
    def _hydrate_node_lite(row: sqlite3.Row) -> Node:
        return Node(id=row["n_id"], kind=row["n_kind"], name=row["n_name"])

    def _hydrate_edge(self, row: sqlite3.Row) -> Edge:
        eid = row["id"]
        props = {
            r["key"]: json.loads(r["value_json"])
            for r in self.conn.execute(
                "SELECT key, value_json FROM edge_properties WHERE edge_id=?", (eid,)
            ).fetchall()
        }
        return Edge(
            id=eid,
            from_node=row["from_node"],
            to_node=row["to_node"],
            type=row["type"],
            properties=props,
        )

    # ---- recursive SQL helper (for advanced traversal) -------------

    def descendants(self, node_id: str, edge_type: str, max_depth: int = 6) -> list[Node]:
        """All nodes reachable from `node_id` by following only `edge_type` edges."""
        rows = self.conn.execute(
            """
            WITH RECURSIVE walk(depth, node_id) AS (
                SELECT 0, ?
                UNION ALL
                SELECT walk.depth + 1, e.to_node
                FROM walk JOIN edges e ON e.from_node = walk.node_id
                WHERE e.type = ? AND walk.depth < ?
            )
            SELECT DISTINCT n.* FROM walk JOIN nodes n ON n.id = walk.node_id
            WHERE walk.depth > 0
            """,
            (node_id, edge_type, max_depth),
        ).fetchall()
        return [self._hydrate_node(r) for r in rows]
