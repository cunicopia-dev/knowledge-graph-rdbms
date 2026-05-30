"""An append-only event log over the graph.

The graph you query is a projection. This append-only log is the source of
truth for every mutation applied at runtime. Replaying the log rebuilds the
projection from scratch (optionally on top of a deterministic "genesis"
seed), which buys you three things at once:

  * audit-as-archaeology — replay the graph to any point in time
  * reversibility — a reversal is a new compensating event, never a delete
    (the log never loses a row)
  * safe automation — every mutation is a replayable, timestamped,
    attributable event

Events are stored in the same SQLite file as the graph (one file). The log
shares the graph's connection so within-process writes don't fight WAL locks.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:  # avoid a circular import at runtime
    from kgrdbms.graph import Graph


_SCHEMA = """
CREATE TABLE IF NOT EXISTS graph_events (
    id            TEXT PRIMARY KEY,
    seq           INTEGER,                       -- monotonic; assigned from rowid
    ts            TEXT NOT NULL,
    actor         TEXT NOT NULL,
    op            TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    compensates   TEXT,                          -- event id this one reverses
    reverted_by   TEXT                           -- event id that reversed this one
);
CREATE INDEX IF NOT EXISTS idx_events_ts  ON graph_events(ts);
CREATE INDEX IF NOT EXISTS idx_events_op  ON graph_events(op);
"""


# Op vocabulary. Single-element mutations plus a batch op and a genesis marker.
OP_NODE_UPSERT = "NODE_UPSERT"
OP_NODE_DELETE = "NODE_DELETE"
OP_NODE_SET_LABEL = "NODE_SET_LABEL"
OP_NODE_REMOVE_LABEL = "NODE_REMOVE_LABEL"
OP_NODE_SET_PROPERTY = "NODE_SET_PROPERTY"
OP_NODE_DEL_PROPERTY = "NODE_DEL_PROPERTY"
OP_EDGE_ADD = "EDGE_ADD"
OP_EDGE_REMOVE = "EDGE_REMOVE"
OP_RESTORE = "RESTORE"        # re-create a captured node + its edges (used to undo a delete)
OP_BATCH = "BATCH"            # add many nodes + edges in one event
OP_GENESIS = "GENESIS"

# Sentinel for "this property did not exist before".
_MISSING = {"__missing__": True}


@dataclass
class GraphEvent:
    id: str
    seq: int
    ts: str
    actor: str
    op: str
    payload: dict[str, Any]
    compensates: str | None = None
    reverted_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "seq": self.seq,
            "ts": self.ts,
            "actor": self.actor,
            "op": self.op,
            "payload": self.payload,
            "compensates": self.compensates,
            "reverted_by": self.reverted_by,
        }


# ---- node/edge spec helpers -----------------------------------------


def node_spec(node) -> dict[str, Any]:
    """Serialize a Node to a replay-safe spec."""
    return {
        "id": node.id,
        "kind": node.kind,
        "name": node.name,
        "labels": sorted(node.labels),
        "properties": node.properties,
    }


def edge_spec(edge) -> dict[str, Any]:
    return {
        "from": edge.from_node,
        "to": edge.to_node,
        "type": edge.type,
        "properties": edge.properties,
    }


class EventLog:
    """Append-only event log over a SQLite store, applied to a projection.

    Two roles that coincide for SQLite but split for other engines:

      * **store** — where the log rows live. Must expose a SQLite `.conn` and a
        `.tx()` transaction context. For a SQLite-backed ontology this *is* the
        graph (the log shares its file). For a non-SQLite ontology it's a
        control-plane SQLite store (see `resolver`), keeping audit/replay/undo
        working even though the graph data lives elsewhere.
      * **projection** — the `GraphBackend` that `compensate()` applies inverse
        events to. Defaults to the store, so `EventLog(graph)` is unchanged.

    `apply_event` only touches `GraphBackend` methods, so compensation and replay
    work against any backend once storage is decoupled from the projection.
    """

    def __init__(self, store: "Graph", projection: Any = None) -> None:
        self.store = store
        self.conn = store.conn
        self.projection = projection if projection is not None else store
        self.graph = self.projection  # back-compat alias for external readers
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ---- write -----------------------------------------------------

    def record(
        self,
        actor: str,
        op: str,
        payload: dict[str, Any],
        *,
        compensates: str | None = None,
    ) -> GraphEvent:
        eid = uuid.uuid4().hex
        ts = datetime.now(timezone.utc).isoformat()
        with self.store.tx():
            cur = self.conn.execute(
                "INSERT INTO graph_events(id, ts, actor, op, payload_json, compensates) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (eid, ts, actor, op, json.dumps(payload), compensates),
            )
            seq = cur.lastrowid
            self.conn.execute("UPDATE graph_events SET seq=? WHERE id=?", (seq, eid))
            if compensates:
                self.conn.execute(
                    "UPDATE graph_events SET reverted_by=? WHERE id=?", (eid, compensates)
                )
        return GraphEvent(id=eid, seq=seq, ts=ts, actor=actor, op=op, payload=payload,
                          compensates=compensates)

    # ---- read ------------------------------------------------------

    def get(self, event_id: str) -> GraphEvent | None:
        row = self.conn.execute("SELECT * FROM graph_events WHERE id=?", (event_id,)).fetchone()
        return self._hydrate(row) if row else None

    def tail(self, n: int = 20) -> list[GraphEvent]:
        rows = self.conn.execute(
            "SELECT * FROM graph_events ORDER BY seq DESC LIMIT ?", (n,)
        ).fetchall()
        return [self._hydrate(r) for r in reversed(rows)]

    def all(self, *, upto_ts: str | None = None) -> list[GraphEvent]:
        if upto_ts:
            rows = self.conn.execute(
                "SELECT * FROM graph_events WHERE ts<=? ORDER BY seq ASC", (upto_ts,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM graph_events ORDER BY seq ASC"
            ).fetchall()
        return [self._hydrate(r) for r in rows]

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS c FROM graph_events").fetchone()["c"]

    def _hydrate(self, row) -> GraphEvent:
        return GraphEvent(
            id=row["id"],
            seq=row["seq"] if row["seq"] is not None else 0,
            ts=row["ts"],
            actor=row["actor"],
            op=row["op"],
            payload=json.loads(row["payload_json"]),
            compensates=row["compensates"],
            reverted_by=row["reverted_by"],
        )

    # ---- compensation (reversal as a new event) --------------------

    def compensate(self, event_id: str, actor: str = "operator") -> GraphEvent:
        """Emit and apply the inverse of an event. The original row is never deleted.

        The graph projection is updated live so the reversal is visible immediately.
        """
        ev = self.get(event_id)
        if ev is None:
            raise KeyError(f"no event {event_id!r}")
        if ev.reverted_by:
            raise ValueError(f"event {event_id!r} already reverted by {ev.reverted_by}")

        inv_op, inv_payload = self._invert(ev)
        comp = self.record(actor, inv_op, inv_payload, compensates=event_id)
        apply_event(self.projection, comp)  # reflect in the projection now
        return comp

    @staticmethod
    def _invert(ev: GraphEvent) -> tuple[str, dict[str, Any]]:
        op, p = ev.op, ev.payload
        if op == OP_EDGE_ADD:
            return OP_EDGE_REMOVE, {"edge": p["edge"]}
        if op == OP_EDGE_REMOVE:
            return OP_EDGE_ADD, {"edge": p["edge"]}
        if op == OP_NODE_UPSERT:
            prior = p.get("prior")
            if prior is None:
                return OP_NODE_DELETE, {"node": p["after"], "edges": []}
            return OP_NODE_UPSERT, {"after": prior, "prior": p["after"]}
        if op == OP_NODE_DELETE:
            return OP_RESTORE, {"node": p["node"], "edges": p.get("edges", [])}
        if op == OP_NODE_SET_LABEL:
            return OP_NODE_REMOVE_LABEL, {"id": p["id"], "label": p["label"]}
        if op == OP_NODE_REMOVE_LABEL:
            return OP_NODE_SET_LABEL, {"id": p["id"], "label": p["label"]}
        if op == OP_NODE_SET_PROPERTY:
            prior = p.get("prior", _MISSING)
            if prior == _MISSING:
                return OP_NODE_DEL_PROPERTY, {"id": p["id"], "key": p["key"]}
            return OP_NODE_SET_PROPERTY, {"id": p["id"], "key": p["key"], "value": prior}
        raise ValueError(f"op {op!r} is not reversible (yet)")


# ---- projection: apply one event to the graph -----------------------


def apply_event(graph: "Graph", ev: GraphEvent) -> None:
    """Apply a single event's effect to the live graph (the projection)."""
    op, p = ev.op, ev.payload
    if op == OP_GENESIS:
        return  # genesis is replayed by re-running the genesis seed, not by this op
    if op in (OP_NODE_UPSERT,):
        spec = p["after"]
        graph.add_node(spec["id"], spec["kind"], spec["name"],
                       labels=spec.get("labels", []), properties=spec.get("properties", {}))
    elif op == OP_NODE_DELETE:
        graph.delete_node(p["node"]["id"])
    elif op == OP_RESTORE:
        spec = p["node"]
        graph.add_node(spec["id"], spec["kind"], spec["name"],
                       labels=spec.get("labels", []), properties=spec.get("properties", {}))
        for e in p.get("edges", []):
            graph.add_edge(e["from"], e["to"], e["type"], e.get("properties", {}))
    elif op == OP_NODE_SET_LABEL:
        graph.add_label(p["id"], p["label"])
    elif op == OP_NODE_REMOVE_LABEL:
        graph.remove_label(p["id"], p["label"])
    elif op == OP_NODE_SET_PROPERTY:
        graph.set_property(p["id"], p["key"], p["value"])
    elif op == OP_NODE_DEL_PROPERTY:
        graph.del_property(p["id"], p["key"])
    elif op == OP_EDGE_ADD:
        e = p["edge"]
        graph.add_edge(e["from"], e["to"], e["type"], e.get("properties", {}))
    elif op == OP_EDGE_REMOVE:
        e = p["edge"]
        graph.delete_edge(e["from"], e["to"], e["type"])
    elif op == OP_BATCH:
        for spec in p.get("nodes", []):
            graph.add_node(spec["id"], spec["kind"], spec["name"],
                           labels=spec.get("labels", []), properties=spec.get("properties", {}))
        for e in p.get("edges", []):
            graph.add_edge(e["from"], e["to"], e["type"], e.get("properties", {}))
    else:  # pragma: no cover - defensive
        raise ValueError(f"cannot apply unknown op {op!r}")


def replay(
    graph: "Graph",
    events: EventLog,
    *,
    upto_ts: str | None = None,
    genesis: Callable[["Graph"], None] | None = None,
) -> dict[str, Any]:
    """Rebuild the graph projection from scratch: optional genesis, then events.

    The graph is cleared, then `genesis(graph)` is invoked if provided (use it
    to re-seed deterministic state from an external source of truth — e.g. YAML
    files), then events are applied in order up to `upto_ts`.

    The event log itself is NOT cleared — it is the source of truth. Only the
    projection is rebuilt. Returns a small report.
    """
    graph.clear()
    if genesis is not None:
        genesis(graph)
    genesis_nodes = graph.total_nodes()

    applied = 0
    for ev in events.all(upto_ts=upto_ts):
        if ev.op == OP_GENESIS:
            continue
        apply_event(graph, ev)
        applied += 1
    return {
        "genesis_nodes": genesis_nodes,
        "events_applied": applied,
        "upto_ts": upto_ts or "HEAD",
    }
