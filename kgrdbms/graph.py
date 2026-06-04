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


# ---- bulk helpers ----------------------------------------------------

# Keep IN(...) parameter lists comfortably under SQLite's variable limit
# (999 on older builds), independent of the SQLite version in use.
_IN_CHUNK = 900


def _chunks(seq: list, size: int = _IN_CHUNK) -> Iterator[list]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _normalize_node(spec: "Node | dict") -> tuple[str, str, str, list[str], dict[str, Any]]:
    """Coerce a node spec (Node or dict) to (id, kind, name, labels, properties)."""
    if isinstance(spec, Node):
        return spec.id, spec.kind, spec.name or spec.id, list(spec.labels), dict(spec.properties)
    if isinstance(spec, dict):
        nid = spec["id"]
        return (
            nid,
            spec["kind"],
            spec.get("name") or nid,
            list(spec.get("labels", [])),
            dict(spec.get("properties", {})),
        )
    raise TypeError(f"node spec must be a Node or dict, got {type(spec).__name__}")


def _normalize_edge(spec: "Edge | dict | tuple | list") -> tuple[str, str, str, dict[str, Any]]:
    """Coerce an edge spec to (from, to, type, properties).

    Accepts an Edge, a dict ({"from","to","type","properties"} — also tolerates
    "from_node"/"to_node"), or a (from, to, type[, properties]) tuple/list.
    """
    if isinstance(spec, Edge):
        return spec.from_node, spec.to_node, spec.type, dict(spec.properties)
    if isinstance(spec, dict):
        return (
            spec.get("from", spec.get("from_node")),
            spec.get("to", spec.get("to_node")),
            spec["type"],
            dict(spec.get("properties", {})),
        )
    if isinstance(spec, (tuple, list)) and len(spec) in (3, 4):
        f, t, typ = spec[0], spec[1], spec[2]
        props = dict(spec[3] or {}) if len(spec) == 4 else {}
        return f, t, typ, props
    raise TypeError("edge spec must be an Edge, dict, or (from, to, type[, properties]) tuple")


def _scalar_samples(values: Iterable[Any], *, max_str: int = 80) -> list | None:
    """Bounded distinct scalar values for schema sampling.

    Returns the values sorted, or None to signal "not an enumerable vocabulary"
    — i.e. some value is a list/object or an over-long string, so showing it as a
    closed set would mislead. Keeps `schema(samples=True)` from dumping free-text.
    """
    out: list = []
    for v in values:
        if isinstance(v, str):
            if len(v) > max_str:
                return None
            out.append(v)
        elif isinstance(v, (int, float)):  # bool is a subclass of int — included
            out.append(v)
        else:  # list / dict / None
            return None
    return sorted(out, key=str) if out else None


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
        self._batch_depth = 0  # >0 means commits are deferred until the batch exits
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

    def _maybe_commit(self) -> None:
        """Commit unless we are inside a batch (then the batch commits once)."""
        if self._batch_depth == 0:
            self.conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Explicit transaction wrapper. Commits on success, rolls back on raise.

        Inside a `batch()` block this defers to the batch: it yields without
        committing, so the whole batch lands (or rolls back) atomically.
        """
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
    def batch(self) -> Iterator["Graph"]:
        """Defer commits until the block exits — one commit for the whole block.

        Every write method commits per call by default (one fsync each), which
        is the right safe default but caps bulk throughput. Wrapping writes in a
        `batch()` collapses them into a single transaction:

            with g.batch():
                for spec in millions:
                    g.add_node(**spec)

        Nestable; only the outermost block commits. Rolls back the whole block
        on exception.
        """
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
        self._maybe_commit()
        return Node(id=id, kind=kind, name=name, labels=set(labels), properties=properties)

    def add_nodes(self, specs: Iterable["Node | dict"]) -> int:
        """Bulk insert/update nodes in a single transaction. Returns the count.

        Each spec is a Node or a dict with `id` and `kind` (plus optional
        `name`, `labels`, `properties`). Semantics match add_node — idempotent
        on id, labels accumulate, properties upsert — but the whole batch
        commits once via executemany instead of once per node.
        """
        node_rows: list[tuple[str, str, str]] = []
        label_rows: list[tuple[str, str]] = []
        prop_rows: list[tuple[str, str, str]] = []
        count = 0
        for spec in specs:
            nid, kind, name, labels, props = _normalize_node(spec)
            node_rows.append((nid, kind, name))
            for label in set(labels):
                label_rows.append((nid, label))
            for k, v in props.items():
                prop_rows.append((nid, k, json.dumps(v)))
            count += 1
        if not node_rows:
            return 0
        with self.tx():
            self.conn.executemany(
                "INSERT INTO nodes(id, kind, name) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET kind=excluded.kind, name=excluded.name",
                node_rows,
            )
            if label_rows:
                self.conn.executemany(
                    "INSERT OR IGNORE INTO node_labels(node_id, label) VALUES (?, ?)",
                    label_rows,
                )
            if prop_rows:
                self.conn.executemany(
                    "INSERT INTO node_properties(node_id, key, value_json) VALUES (?, ?, ?) "
                    "ON CONFLICT(node_id, key) DO UPDATE SET value_json=excluded.value_json",
                    prop_rows,
                )
        return count

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
        self._maybe_commit()
        return Edge(id=edge_id, from_node=from_node, to_node=to_node, type=type, properties=props)

    def add_edges(self, specs: Iterable["Edge | dict | tuple | list"]) -> int:
        """Bulk add edges in a single transaction. Returns the number processed.

        Each spec is an Edge, a dict ({"from","to","type","properties"}), or a
        (from, to, type[, properties]) tuple. Triples (from, type, to) stay
        unique: an existing triple is left in place and its properties upserted.
        Both endpoint nodes must already exist (foreign keys are enforced).
        """
        edge_rows: list[tuple[str, str, str, str]] = []          # (id, from, to, type)
        prop_rows: list[tuple[str, str, str, str, str]] = []     # (from, type, to, key, value_json)
        count = 0
        for spec in specs:
            f, t, typ, props = _normalize_edge(spec)
            edge_rows.append((uuid.uuid4().hex, f, t, typ))
            for k, v in props.items():
                prop_rows.append((f, typ, t, k, json.dumps(v)))
            count += 1
        if not edge_rows:
            return 0
        with self.tx():
            # OR IGNORE: the unique triple index keeps the first id for a triple;
            # a generated id for a triple that already exists is simply dropped.
            self.conn.executemany(
                "INSERT OR IGNORE INTO edges(id, from_node, to_node, type) VALUES (?, ?, ?, ?)",
                edge_rows,
            )
            if prop_rows:
                # Resolve each triple to its actual edge id via a subquery so
                # properties attach to the surviving row, not a dropped one.
                self.conn.executemany(
                    "INSERT INTO edge_properties(edge_id, key, value_json) VALUES "
                    "((SELECT id FROM edges WHERE from_node=? AND type=? AND to_node=?), ?, ?) "
                    "ON CONFLICT(edge_id, key) DO UPDATE SET value_json=excluded.value_json",
                    prop_rows,
                )
        return count

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
        return self._hydrate_nodes(rows)

    def nodes_by_label(self, label: str) -> list[Node]:
        rows = self.conn.execute(
            "SELECT n.* FROM nodes n JOIN node_labels l ON l.node_id = n.id "
            "WHERE l.label = ? ORDER BY n.id",
            (label,),
        ).fetchall()
        return self._hydrate_nodes(rows)

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
        return self._rows_to_edge_node_pairs(rows)

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
        return self._rows_to_edge_node_pairs(rows)

    def neighborhood(self, node_id: str, depth: int = 1) -> dict[str, Node]:
        """All nodes reachable from node_id within `depth` undirected hops (inclusive of self).

        BFS walks over ids only (cheap — out/in_ return lightweight target
        nodes), then the whole reachable set is hydrated in one bulk pass rather
        than re-fetching each node individually.
        """
        depth = max(0, depth)
        if self.conn.execute("SELECT 1 FROM nodes WHERE id=?", (node_id,)).fetchone() is None:
            return {}
        seen_ids: set[str] = {node_id}
        frontier = {node_id}
        for _ in range(depth):
            next_frontier: set[str] = set()
            for nid in frontier:
                for _, neighbor in self.out(nid):
                    if neighbor.id not in seen_ids:
                        seen_ids.add(neighbor.id)
                        next_frontier.add(neighbor.id)
                for _, neighbor in self.in_(nid):
                    if neighbor.id not in seen_ids:
                        seen_ids.add(neighbor.id)
                        next_frontier.add(neighbor.id)
            frontier = next_frontier
            if not frontier:
                break
        rows: list[sqlite3.Row] = []
        ids = list(seen_ids)
        for chunk in _chunks(ids):
            placeholders = ",".join("?" * len(chunk))
            rows.extend(
                self.conn.execute(
                    f"SELECT * FROM nodes WHERE id IN ({placeholders})", chunk
                ).fetchall()
            )
        return {n.id: n for n in self._hydrate_nodes(rows)}

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

    # ---- schema (observed TBox: the map of what's in the graph) ----

    def schema(self, *, samples: bool = False, sample_limit: int = 20) -> dict:
        """The observed schema of the graph — the vocabulary needed to query it
        without guessing.

        Returns kinds, edge types, labels, and property keys *per kind*, each
        with counts. This is a profile of what actually occurs (the graph is
        schemaless — nothing is enforced), the property-graph analogue of an
        ontology's TBox derived from its ABox.

        `samples=True` additionally returns, per kind, a few example node ids
        (revealing the id/CURIE convention) and — for enum-like properties — the
        bounded set of distinct scalar values a key takes, turning "there is a
        key `status`" into "status is one of {active, archived}". A key whose
        distinct values exceed `sample_limit` (a free-text field) is left
        un-enumerated rather than dumped.

        Read-only; pure GROUP BY aggregates. The intended first call for any
        consumer dropped into an unfamiliar ontology.
        """
        kinds = self.count_nodes_by_kind()
        edge_types = self.count_edges_by_type()
        labels = {
            r["label"]: r["c"]
            for r in self.conn.execute(
                "SELECT label, COUNT(*) AS c FROM node_labels GROUP BY label ORDER BY c DESC"
            ).fetchall()
        }
        node_keys_by_kind: dict[str, dict[str, int]] = {}
        for r in self.conn.execute(
            "SELECT n.kind AS kind, p.key AS key, COUNT(*) AS c "
            "FROM node_properties p JOIN nodes n ON n.id = p.node_id "
            "GROUP BY n.kind, p.key ORDER BY n.kind, c DESC"
        ).fetchall():
            node_keys_by_kind.setdefault(r["kind"], {})[r["key"]] = r["c"]
        edge_keys = {
            r["key"]: r["c"]
            for r in self.conn.execute(
                "SELECT key, COUNT(*) AS c FROM edge_properties GROUP BY key ORDER BY c DESC"
            ).fetchall()
        }
        result: dict[str, Any] = {
            "nodes_total": self.total_nodes(),
            "edges_total": self.total_edges(),
            "kinds": kinds,
            "edge_types": edge_types,
            "labels": labels,
            "node_keys_by_kind": node_keys_by_kind,
            "edge_keys": edge_keys,
        }
        if samples:
            result["samples"] = self._schema_samples(kinds, node_keys_by_kind, sample_limit)
        return result

    def _schema_samples(
        self, kinds: dict[str, int], node_keys_by_kind: dict[str, dict[str, int]], sample_limit: int
    ) -> dict[str, dict]:
        samples: dict[str, dict] = {}
        for kind in kinds:
            example_ids = [
                r["id"]
                for r in self.conn.execute(
                    "SELECT id FROM nodes WHERE kind=? ORDER BY id LIMIT 5", (kind,)
                ).fetchall()
            ]
            values: dict[str, list] = {}
            for key in node_keys_by_kind.get(kind, {}):
                rows = self.conn.execute(
                    "SELECT DISTINCT p.value_json FROM node_properties p "
                    "JOIN nodes n ON n.id = p.node_id "
                    "WHERE n.kind=? AND p.key=? LIMIT ?",
                    (kind, key, sample_limit + 1),
                ).fetchall()
                if len(rows) > sample_limit:
                    continue  # open-ended / free-text field — don't enumerate it
                vals = _scalar_samples(json.loads(r["value_json"]) for r in rows)
                if vals is not None:
                    values[key] = vals
            samples[kind] = {"example_ids": example_ids, "values": values}
        return samples

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

    def _hydrate_nodes(self, rows: Iterable[sqlite3.Row]) -> list[Node]:
        """Hydrate many node rows with two queries total instead of two per row.

        The naive path fetches a node's labels and properties in a follow-up
        query each — O(rows) round-trips (the N+1 problem). Here we fetch every
        label and every property for the whole result set in two chunked IN()
        queries, group them in Python, and assemble the Nodes in row order.
        """
        rows = list(rows)
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        labels_by: dict[str, set[str]] = {nid: set() for nid in ids}
        props_by: dict[str, dict[str, Any]] = {nid: {} for nid in ids}
        for chunk in _chunks(ids):
            placeholders = ",".join("?" * len(chunk))
            for r in self.conn.execute(
                f"SELECT node_id, label FROM node_labels WHERE node_id IN ({placeholders})",
                chunk,
            ):
                labels_by[r["node_id"]].add(r["label"])
            for r in self.conn.execute(
                f"SELECT node_id, key, value_json FROM node_properties "
                f"WHERE node_id IN ({placeholders})",
                chunk,
            ):
                props_by[r["node_id"]][r["key"]] = json.loads(r["value_json"])
        return [
            Node(
                id=r["id"],
                kind=r["kind"],
                name=r["name"],
                labels=labels_by[r["id"]],
                properties=props_by[r["id"]],
            )
            for r in rows
        ]

    def _rows_to_edge_node_pairs(self, rows: Iterable[sqlite3.Row]) -> list[tuple[Edge, Node]]:
        """Build (Edge, lite Node) pairs from out()/in_() rows, bulk-fetching edge
        properties in one query instead of one per edge."""
        rows = list(rows)
        if not rows:
            return []
        eids = [r["id"] for r in rows]
        props_by: dict[str, dict[str, Any]] = {eid: {} for eid in eids}
        for chunk in _chunks(eids):
            placeholders = ",".join("?" * len(chunk))
            for pr in self.conn.execute(
                f"SELECT edge_id, key, value_json FROM edge_properties "
                f"WHERE edge_id IN ({placeholders})",
                chunk,
            ):
                props_by[pr["edge_id"]][pr["key"]] = json.loads(pr["value_json"])
        return [
            (
                Edge(
                    id=r["id"],
                    from_node=r["from_node"],
                    to_node=r["to_node"],
                    type=r["type"],
                    properties=props_by[r["id"]],
                ),
                Node(id=r["n_id"], kind=r["n_kind"], name=r["n_name"]),
            )
            for r in rows
        ]

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
        return self._hydrate_nodes(rows)
