"""Virtual edges — graph relationships resolved on demand from an external SQL
source, never stored in the projection.

The graph normally *materializes* every edge: a row in `edges`, gated and
event-logged. That is the right model for curated facts, but it is the wrong
model for a high-cardinality, machine-generated relationship layer that already
lives — fresh and authoritative — in some operational store. Mirroring 100k
correlation edges into the graph (and the event log) every night is wasteful and
immediately stale.

A *virtual edge* inverts that. The ontology stores only a **binding**: an edge
TYPE plus the SQL that resolves its instances against an external source. At
traversal time, the resolver runs that query parameterized by the node you're
standing on and synthesizes the edges live. Zero copy, always current, single
source of truth — Ontology-Based Data Access (OBDA) in the graph's own terms.

Two properties keep this safe and simple:

  * **Read-only.** Virtual edges are never written, so they sidestep the entire
    gated-write / event-sourcing / compensation machinery. There is nothing to
    invalidate, replay, or undo. A binding is config; the edges are a view.

  * **Parameterized, never interpolated.** The SQL template is operator-authored
    (trusted); the per-node value is always *bound* through the driver, never
    string-formatted into the query. The one genuinely new surface — reaching
    out to an arbitrary SQL source — carries credentials by env-var reference
    (`dsn_env`), so secrets stay out of the graph and out of version control.

Bindings live as reserved-kind (`_VirtualEdge`) nodes in the ontology itself, so
they travel with it, version in kgvault alongside the schema, and are
discoverable via the ordinary read tools.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from kgrdbms.graph import Edge, Node

# Reserved node kind for binding config. The leading underscore marks it as
# machinery, not domain data — same spirit as the index graph's "Ontology" kind.
BINDING_KIND = "_VirtualEdge"
_BINDING_PREFIX = "_virtual_edge"


def binding_id(edge_type: str) -> str:
    """One binding per edge type per ontology — a stable, addressable id."""
    return f"{_BINDING_PREFIX}:{edge_type}"


@dataclass
class VirtualEdge:
    """How an edge TYPE is resolved from an external SQL source.

    edge_type          the synthetic relationship name (e.g. "CO_HELD_WITH").
    query              SQL returning one row per resolved edge. It must contain
                       exactly one placeholder, bound to the source value — the
                       driver's native marker: '?' for sqlite, '%s' for postgres.
    dsn / dsn_env      the source. Prefer `dsn_env` (an env-var *name*) so
                       credentials never touch the graph; `dsn` is the literal
                       form, fine for non-secret sqlite paths.
    source             how to derive the bound value from the node you traverse
                       from: "id" (default), "id_slug" (part after the last ':'),
                       or "prop:<key>" (a node property, e.g. "prop:ticker").
    target_col         result column holding the far-end value. Default "to_id".
    target_id_template how to build the target node id from that value, e.g.
                       "company:{value}". Default "{value}" (use the value as-is).
    target_kind        kind stamped on the synthesized far-end node.
    name_col           optional result column for the target node's display name.
    prop_cols          result columns to attach as edge properties. Empty = every
                       column except the target/name columns.
    directions         which traversal directions this binding answers: "out"
                       (default), "in", or "both". The query always takes the
                       traversed node's value; "in"/"both" simply re-orient the
                       synthesized edge so inbound traversal sees it.
    """

    edge_type: str
    query: str
    dsn: str | None = None
    dsn_env: str | None = None
    source: str = "id"
    target_col: str = "to_id"
    target_id_template: str = "{value}"
    target_kind: str = "external"
    name_col: str | None = None
    prop_cols: list[str] = field(default_factory=list)
    directions: str = "out"

    # ---- (de)serialization to a reserved-kind node ----

    def to_node_properties(self) -> dict[str, Any]:
        return {
            "edge_type": self.edge_type,
            "query": self.query,
            "dsn": self.dsn,
            "dsn_env": self.dsn_env,
            "source": self.source,
            "target_col": self.target_col,
            "target_id_template": self.target_id_template,
            "target_kind": self.target_kind,
            "name_col": self.name_col,
            "prop_cols": list(self.prop_cols),
            "directions": self.directions,
        }

    @classmethod
    def from_node(cls, node: Node) -> "VirtualEdge":
        p = node.properties
        return cls(
            edge_type=p["edge_type"],
            query=p["query"],
            dsn=p.get("dsn"),
            dsn_env=p.get("dsn_env"),
            source=p.get("source", "id"),
            target_col=p.get("target_col", "to_id"),
            target_id_template=p.get("target_id_template", "{value}"),
            target_kind=p.get("target_kind", "external"),
            name_col=p.get("name_col"),
            prop_cols=list(p.get("prop_cols", [])),
            directions=p.get("directions", "out"),
        )

    def resolved_dsn(self) -> str:
        """The connection string, resolving `dsn_env` against the environment."""
        if self.dsn_env:
            val = os.environ.get(self.dsn_env)
            if not val:
                raise RuntimeError(
                    f"virtual edge {self.edge_type!r}: env var {self.dsn_env!r} "
                    f"is unset — cannot reach the external source."
                )
            return val
        if self.dsn:
            return self.dsn
        raise RuntimeError(
            f"virtual edge {self.edge_type!r}: neither `dsn` nor `dsn_env` set."
        )

    def answers(self, direction: str) -> bool:
        """Does this binding contribute edges for a traversal in `direction`?"""
        if self.directions == "both":
            return True
        return self.directions == direction


# ---- the binding registry (reserved-kind nodes in the ontology) ------


def register(backend: Any, ve: VirtualEdge) -> VirtualEdge:
    """Store (or replace) a binding. Config, written direct — not gated user data."""
    backend.add_node(
        id=binding_id(ve.edge_type),
        kind=BINDING_KIND,
        name=ve.edge_type,
        labels=[BINDING_KIND],
        properties=ve.to_node_properties(),
    )
    return ve


def unregister(backend: Any, edge_type: str) -> bool:
    """Remove a binding by edge type. Returns whether one existed."""
    return backend.delete_node(binding_id(edge_type))


def list_bindings(backend: Any) -> list[VirtualEdge]:
    return [VirtualEdge.from_node(n) for n in backend.nodes_by_kind(BINDING_KIND)]


# ---- resolution ------------------------------------------------------


def _connect(dsn: str):
    """Open the source and report its positional placeholder marker.

    Postgres goes through psycopg (the `postgres` extra); everything else is a
    sqlite path (optionally `sqlite://`-prefixed). The marker lets `_bind`
    validate the query came written for the right driver.
    """
    if dsn.startswith(("postgresql://", "postgres://")):
        try:
            import psycopg  # type: ignore
            from psycopg.rows import dict_row  # type: ignore
        except ModuleNotFoundError as e:  # pragma: no cover - env dependent
            raise RuntimeError(
                "virtual edge needs the postgres driver: pip install "
                "'knowledge-graph-rdbms[postgres]'"
            ) from e
        # dict_row so fetchall() yields mappings — resolve() does dict(row) on
        # each, which would fail on psycopg's default tuple rows.
        return psycopg.connect(dsn, row_factory=dict_row), "%s"
    path = dsn
    for prefix in ("sqlite:///", "sqlite://"):
        if path.startswith(prefix):
            path = path[len(prefix) - 1:] if prefix == "sqlite:///" else path[len(prefix):]
            break
    import sqlite3

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn, "?"


def _source_value(ve: VirtualEdge, node_id: str, node: Node | None) -> Any:
    if ve.source == "id":
        return node_id
    if ve.source == "id_slug":
        return node_id.split(":", 1)[-1]
    if ve.source.startswith("prop:"):
        key = ve.source.split(":", 1)[1]
        if node is None:
            return None
        return node.properties.get(key)
    raise ValueError(f"virtual edge {ve.edge_type!r}: unknown source {ve.source!r}")


def _row_to_props(ve: VirtualEdge, row: dict) -> dict[str, Any]:
    if ve.prop_cols:
        return {c: row[c] for c in ve.prop_cols if c in row}
    skip = {ve.target_col, ve.name_col}
    return {k: v for k, v in row.items() if k not in skip}


def resolve(
    ve: VirtualEdge, node_id: str, node: Node | None, direction: str
) -> list[tuple[Edge, Node]]:
    """Run the binding's query for one node and synthesize its edges.

    Returns (Edge, far-end Node) pairs shaped exactly like Graph.out()/in_(), so
    callers can treat virtual and real edges uniformly. The far-end node is a
    lightweight synthesized stub (id, kind, name) — the instance lives in the
    external store, not here.
    """
    value = _source_value(ve, node_id, node)
    if value is None:
        return []
    conn, marker = _connect(ve.resolved_dsn())
    try:
        n_params = ve.query.count(marker)
        cur = conn.execute(ve.query, tuple([value] * max(1, n_params)))
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    pairs: list[tuple[Edge, Node]] = []
    for row in rows:
        target_value = row[ve.target_col]
        target_id = ve.target_id_template.format(value=target_value)
        name = row.get(ve.name_col) if ve.name_col else None
        props = _row_to_props(ve, row)
        props["_virtual"] = True
        far = Node(id=target_id, kind=ve.target_kind, name=name or str(target_value))
        # "in" traversal re-orients the synthesized edge so the far end points in.
        if direction == "in":
            edge = Edge(
                id=f"virtual:{ve.edge_type}:{target_id}->{node_id}",
                from_node=target_id, to_node=node_id, type=ve.edge_type, properties=props,
            )
        else:
            edge = Edge(
                id=f"virtual:{ve.edge_type}:{node_id}->{target_id}",
                from_node=node_id, to_node=target_id, type=ve.edge_type, properties=props,
            )
        pairs.append((edge, far))
    return pairs


def augment(
    backend: Any, node_id: str, direction: str, edge_type: str | None
) -> list[tuple[str, Edge, Node]]:
    """All virtual edges for a node, as (direction, edge, far_node) triples.

    Reads the ontology's bindings, filters to those matching `edge_type` (if
    given) and the requested `direction`, and resolves each. `direction="both"`
    asks every binding for the orientations it answers. Real edges are the
    caller's concern; this returns only the virtual overlay.
    """
    bindings = list_bindings(backend)
    if not bindings:
        return []
    if edge_type is not None:
        bindings = [b for b in bindings if b.edge_type == edge_type]
    if not bindings:
        return []
    node = backend.node(node_id)
    wanted = ("out", "in") if direction == "both" else (direction,)
    out: list[tuple[str, Edge, Node]] = []
    for ve in bindings:
        for d in wanted:
            if not ve.answers(d):
                continue
            for edge, far in resolve(ve, node_id, node, d):
                out.append((d, edge, far))
    return out
