"""MCP server over the graph.

Exposes the label property graph as Model Context Protocol tools so any
MCP-aware client (Claude Code, Claude Desktop, an editor, an agent) can read
and mutate the graph over a wire.

Transports: stdio (default; what Claude Code & Claude Desktop expect).
SSE / streamable-http work too.

Tool surface (all prefixed kg_):

  reads
    kg_stats              — node/edge counts and the db path
    kg_node_get           — fetch a node by id
    kg_nodes_by_kind      — list all nodes of a kind
    kg_nodes_by_label     — list all nodes carrying a label
    kg_edges_out          — outbound edges of a node
    kg_edges_in           — inbound edges of a node
    kg_neighborhood       — undirected BFS within depth
    kg_shortest_path      — shortest path between two ids
    kg_descendants        — recursive walk along one edge type

  writes (gated: invariants.enforce THEN policy.mutation_check)
    kg_node_upsert        — create or update a node
    kg_node_set_label     — add a label to a node
    kg_node_set_property  — set a property on a node
    kg_node_delete        — delete a node and its edges
    kg_edge_add           — add a (from, type, to) edge
    kg_edge_remove        — remove a (from, type, to) edge

  event log
    kg_events_tail        — most recent events
    kg_event_revert       — reverse an event via a compensating event
    kg_replay             — rebuild the projection from the log (time travel)

Storage: ~/.kgrdbms/graph.db, or set KGRDBMS_HOME.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from kgrdbms import service
from kgrdbms.events import EventLog
from kgrdbms.graph import Edge, Graph, Node


# ---- process-lifetime singletons ------------------------------------
#
# FastMCP runs as a single process per client. Open the graph + event log
# once at import time so tool calls are cheap. SQLite WAL handles concurrent
# reads/writes from this process.

_GRAPH = Graph()
_EVENTS = EventLog(_GRAPH)

mcp = FastMCP(
    name="kgrdbms",
    instructions=(
        "A SQLite label property graph. Tools prefixed kg_ read or mutate "
        "nodes (id, kind, name, labels, properties) and typed directed edges. "
        "Writes are gated by compiled-in invariants and a configurable policy, "
        "and recorded to an append-only, replayable event log."
    ),
)


# ---- serialization helpers ------------------------------------------


def _node_to_dict(n: Node | None) -> dict | None:
    if n is None:
        return None
    return {
        "id": n.id,
        "kind": n.kind,
        "name": n.name,
        "labels": sorted(n.labels),
        "properties": n.properties,
    }


def _edge_to_dict(e: Edge) -> dict:
    return {
        "id": e.id,
        "from": e.from_node,
        "to": e.to_node,
        "type": e.type,
        "properties": e.properties,
    }


# =====================================================================
# READS
# =====================================================================


@mcp.tool()
def kg_stats() -> dict:
    """Counts of nodes by kind and edges by type, plus totals and the db path."""
    return {
        "nodes_total": _GRAPH.total_nodes(),
        "edges_total": _GRAPH.total_edges(),
        "nodes_by_kind": _GRAPH.count_nodes_by_kind(),
        "edges_by_type": _GRAPH.count_edges_by_type(),
        "db_path": str(_GRAPH.path),
    }


@mcp.tool()
def kg_node_get(id: str) -> dict | None:
    """Fetch a single node by id."""
    return _node_to_dict(_GRAPH.node(id))


@mcp.tool()
def kg_nodes_by_kind(kind: str) -> list[dict]:
    """List all nodes of a given kind."""
    return [_node_to_dict(n) for n in _GRAPH.nodes_by_kind(kind)]


@mcp.tool()
def kg_nodes_by_label(label: str) -> list[dict]:
    """List all nodes carrying a label."""
    return [_node_to_dict(n) for n in _GRAPH.nodes_by_label(label)]


@mcp.tool()
def kg_edges_out(id: str, edge_type: str | None = None) -> list[dict]:
    """Outbound edges from a node, optionally filtered by edge type."""
    out = []
    for edge, target in _GRAPH.out(id, edge_type):
        d = _edge_to_dict(edge)
        d["target"] = _node_to_dict(target)
        out.append(d)
    return out


@mcp.tool()
def kg_edges_in(id: str, edge_type: str | None = None) -> list[dict]:
    """Inbound edges into a node, optionally filtered by edge type."""
    out = []
    for edge, source in _GRAPH.in_(id, edge_type):
        d = _edge_to_dict(edge)
        d["source"] = _node_to_dict(source)
        out.append(d)
    return out


@mcp.tool()
def kg_neighborhood(id: str, depth: int = 1) -> list[dict]:
    """Undirected BFS from a node within `depth` hops (inclusive of self)."""
    return [_node_to_dict(n) for n in _GRAPH.neighborhood(id, depth=depth).values()]


@mcp.tool()
def kg_shortest_path(from_id: str, to_id: str, max_depth: int = 8) -> list[dict] | None:
    """Shortest undirected path between two node ids, or None if no path."""
    path = _GRAPH.shortest_path(from_id, to_id, max_depth=max_depth)
    return [_node_to_dict(n) for n in path] if path else None


@mcp.tool()
def kg_descendants(id: str, edge_type: str, max_depth: int = 6) -> list[dict]:
    """All nodes reachable from `id` by following only `edge_type` edges."""
    return [_node_to_dict(n) for n in _GRAPH.descendants(id, edge_type, max_depth=max_depth)]


# =====================================================================
# WRITES (gate = invariants THEN policy; then logged as a replayable event)
# =====================================================================
#
# Every write tool takes an `actor` so the event is attributable. The gate
# (`_guard`) runs compiled-in invariants first, then the configurable policy.
# After a successful write we record the inverse-capable event into the log.


@mcp.tool()
def kg_node_upsert(
    id: str,
    kind: str,
    name: str | None = None,
    labels: list[str] | None = None,
    properties: dict[str, Any] | None = None,
    actor: str = "anonymous",
) -> dict:
    """Create or update a node. Properties are JSON-serializable.

    Gated by invariants then policy. Logged as a reversible NODE_UPSERT event
    (the node's prior state is captured so the upsert can be compensated).
    """
    node = service.upsert_node(
        _GRAPH, _EVENTS, id=id, kind=kind, name=name, labels=labels, properties=properties, actor=actor
    )
    return _node_to_dict(node)  # type: ignore[return-value]


@mcp.tool()
def kg_node_set_label(id: str, label: str, actor: str = "anonymous") -> dict | None:
    """Add a label to an existing node. Logged + reversible."""
    return _node_to_dict(service.set_label(_GRAPH, _EVENTS, id, label, actor=actor))


@mcp.tool()
def kg_node_set_property(id: str, key: str, value: Any, actor: str = "anonymous") -> dict | None:
    """Set a single property on a node. Value must be JSON-serializable. Logged + reversible."""
    return _node_to_dict(service.set_property(_GRAPH, _EVENTS, id, key, value, actor=actor))


@mcp.tool()
def kg_node_delete(id: str, actor: str = "anonymous") -> dict:
    """Delete a node (cascade removes its edges/properties).

    Captures the full node + its incident edges in the event so the delete can
    be compensated (restored) later.
    """
    return service.delete_node(_GRAPH, _EVENTS, id, actor=actor)


@mcp.tool()
def kg_edge_add(
    from_id: str,
    to_id: str,
    type: str,
    properties: dict[str, Any] | None = None,
    actor: str = "anonymous",
) -> dict:
    """Add an edge. Triples (from, type, to) are unique; repeats update properties."""
    edge = service.add_edge(_GRAPH, _EVENTS, from_id, to_id, type, properties=properties, actor=actor)
    return _edge_to_dict(edge)


@mcp.tool()
def kg_edge_remove(from_id: str, to_id: str, type: str, actor: str = "anonymous") -> dict:
    """Remove a specific edge by (from, type, to). Idempotent. Logged + reversible."""
    return service.remove_edge(_GRAPH, _EVENTS, from_id, to_id, type, actor=actor)


# ---- event log: read + reversal + replay ----------------------------


@mcp.tool()
def kg_events_tail(n: int = 20) -> list[dict]:
    """The most recent events from the append-only log."""
    return [e.to_dict() for e in _EVENTS.tail(n)]


@mcp.tool()
def kg_event_revert(event_id: str, actor: str = "operator") -> dict:
    """Reverse an event by emitting a compensating event. The original is never deleted."""
    return service.revert_event(_EVENTS, event_id, actor=actor).to_dict()


@mcp.tool()
def kg_replay(upto_ts: str | None = None) -> dict:
    """Rebuild the graph projection from the event log.

    With upto_ts (ISO 8601) the graph is projected to that point in time. The
    event log is never cleared.
    """
    return service.replay_log(_GRAPH, _EVENTS, upto_ts=upto_ts)


# =====================================================================
# Entrypoint
# =====================================================================


def serve(transport: str = "stdio") -> None:
    """Run the MCP server. Transports: stdio (default), sse, streamable-http."""
    mcp.run(transport=transport)


def main() -> None:  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(description="MCP server over the kgrdbms graph")
    parser.add_argument(
        "--transport", default="stdio", choices=["stdio", "sse", "streamable-http"]
    )
    args = parser.parse_args()
    serve(transport=args.transport)


if __name__ == "__main__":  # pragma: no cover
    main()
