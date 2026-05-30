"""MCP server over the graph.

Exposes the label property graph as Model Context Protocol tools so any
MCP-aware client (Claude Code, Claude Desktop, an editor, an agent) can read
and mutate the graph over a wire.

Transports: stdio (default; what Claude Code & Claude Desktop expect).
SSE / streamable-http work too.

Multi-ontology: every tool takes an optional `ontology` name. Omit it and you
hit the default ontology (the legacy ~/.kgrdbms/graph.db, unchanged); pass a
name and the resolver routes to that ontology's backend — a different SQLite
file, or eventually a Postgres/Neo4j engine — through the same gate + event log.
`kg_ontologies_list` / `kg_ontology_create` manage the registry (itself a kg).

Tool surface (all prefixed kg_):

  control plane
    kg_ontologies_list    — registered ontologies (the "database of databases")
    kg_ontology_create    — register a new ontology (name, backend, opinion)

  reads
    kg_stats              — node/edge counts, the db path, the active ontology
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

Storage root: ~/.kgrdbms, or set KGRDBMS_HOME. Default ontology: "default", or
set KGRDBMS_DEFAULT_ONTOLOGY.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from kgrdbms import resolver, service
from kgrdbms.graph import Edge, Node
from kgrdbms.resolver import Resolved


# ---- per-ontology bundles, cached for process lifetime --------------
#
# FastMCP runs as a single process per client. We open each ontology's backend
# + event log on first use and keep it for the life of the process (SQLite WAL
# handles concurrent reads/writes from this process). The default ontology maps
# to the legacy graph file, so omitting `ontology` is a zero-change default.

_BUNDLES: dict[str, Resolved] = {}


def _bundle(ontology: str | None) -> Resolved:
    name = ontology or resolver.default_ontology_name()
    cached = _BUNDLES.get(name)
    if cached is None:
        cached = resolver.resolve(name)
        _BUNDLES[name] = cached
    return cached


mcp = FastMCP(
    name="kgrdbms",
    instructions=(
        "A SQLite label property graph with multiple named ontologies. Tools "
        "prefixed kg_ read or mutate nodes (id, kind, name, labels, properties) "
        "and typed directed edges. Every tool takes an optional `ontology` name "
        "(omit for the default); use kg_ontologies_list to discover them and "
        "kg_ontology_create to add one. Writes are gated by compiled-in "
        "invariants and a configurable policy, and recorded to an append-only, "
        "replayable event log."
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
# CONTROL PLANE — the registry of ontologies (itself a kg)
# =====================================================================


@mcp.tool()
def kg_ontologies_list() -> list[dict]:
    """List registered ontologies: name, backend engine, extraction stance, path.

    The registry is itself a knowledge graph; this is a query over it. Use the
    returned `name` as the `ontology` argument on any other tool.
    """
    return [
        {
            "name": e.name,
            "backend": e.backend,
            "stance": e.stance,
            "description": e.description,
            "path": e.path,
        }
        for e in resolver.list_ontologies()
    ]


@mcp.tool()
def kg_ontology_create(
    name: str,
    backend: str = "sqlite",
    description: str = "",
    stance: str = "literal",
) -> dict:
    """Register a new ontology (or update an existing one's metadata).

    `backend` routes the engine ("sqlite" is live; "postgres"/"neo4j" are
    registered stubs). `stance` is the ontology's own extraction opinion
    ("literal" vs "inferential") that a composing agent should honor. The
    ontology becomes immediately addressable via the `ontology` argument.
    """
    entry = resolver.register(name, backend=backend, description=description, stance=stance)
    _BUNDLES.pop(name, None)  # drop any stale cached bundle for this name
    return {
        "name": entry.name,
        "backend": entry.backend,
        "stance": entry.stance,
        "description": entry.description,
        "path": entry.path,
    }


# =====================================================================
# READS
# =====================================================================


@mcp.tool()
def kg_stats(ontology: str | None = None) -> dict:
    """Counts of nodes by kind and edges by type, plus totals, the db path, and
    which ontology/backend served the call."""
    b = _bundle(ontology)
    return {
        "ontology": b.entry.name,
        "backend": b.entry.backend,
        "nodes_total": b.backend.total_nodes(),
        "edges_total": b.backend.total_edges(),
        "nodes_by_kind": b.backend.count_nodes_by_kind(),
        "edges_by_type": b.backend.count_edges_by_type(),
        "db_path": b.entry.path,
    }


@mcp.tool()
def kg_node_get(id: str, ontology: str | None = None) -> dict | None:
    """Fetch a single node by id."""
    return _node_to_dict(_bundle(ontology).backend.node(id))


@mcp.tool()
def kg_nodes_by_kind(kind: str, ontology: str | None = None) -> list[dict]:
    """List all nodes of a given kind."""
    return [_node_to_dict(n) for n in _bundle(ontology).backend.nodes_by_kind(kind)]


@mcp.tool()
def kg_nodes_by_label(label: str, ontology: str | None = None) -> list[dict]:
    """List all nodes carrying a label."""
    return [_node_to_dict(n) for n in _bundle(ontology).backend.nodes_by_label(label)]


@mcp.tool()
def kg_edges_out(id: str, edge_type: str | None = None, ontology: str | None = None) -> list[dict]:
    """Outbound edges from a node, optionally filtered by edge type."""
    out = []
    for edge, target in _bundle(ontology).backend.out(id, edge_type):
        d = _edge_to_dict(edge)
        d["target"] = _node_to_dict(target)
        out.append(d)
    return out


@mcp.tool()
def kg_edges_in(id: str, edge_type: str | None = None, ontology: str | None = None) -> list[dict]:
    """Inbound edges into a node, optionally filtered by edge type."""
    out = []
    for edge, source in _bundle(ontology).backend.in_(id, edge_type):
        d = _edge_to_dict(edge)
        d["source"] = _node_to_dict(source)
        out.append(d)
    return out


@mcp.tool()
def kg_neighborhood(id: str, depth: int = 1, ontology: str | None = None) -> list[dict]:
    """Undirected BFS from a node within `depth` hops (inclusive of self)."""
    return [_node_to_dict(n) for n in _bundle(ontology).backend.neighborhood(id, depth=depth).values()]


@mcp.tool()
def kg_shortest_path(
    from_id: str, to_id: str, max_depth: int = 8, ontology: str | None = None
) -> list[dict] | None:
    """Shortest undirected path between two node ids, or None if no path."""
    path = _bundle(ontology).backend.shortest_path(from_id, to_id, max_depth=max_depth)
    return [_node_to_dict(n) for n in path] if path else None


@mcp.tool()
def kg_descendants(
    id: str, edge_type: str, max_depth: int = 6, ontology: str | None = None
) -> list[dict]:
    """All nodes reachable from `id` by following only `edge_type` edges."""
    return [_node_to_dict(n) for n in _bundle(ontology).backend.descendants(id, edge_type, max_depth=max_depth)]


# =====================================================================
# WRITES (gate = invariants THEN policy; then logged as a replayable event)
# =====================================================================
#
# Every write tool takes an `actor` so the event is attributable, and an
# optional `ontology` selecting which graph to mutate. The gate runs compiled-in
# invariants first, then the configurable policy; after a successful write the
# inverse-capable event is recorded into that ontology's log.


@mcp.tool()
def kg_node_upsert(
    id: str,
    kind: str,
    name: str | None = None,
    labels: list[str] | None = None,
    properties: dict[str, Any] | None = None,
    actor: str = "anonymous",
    ontology: str | None = None,
) -> dict:
    """Create or update a node. Properties are JSON-serializable.

    Gated by invariants then policy. Logged as a reversible NODE_UPSERT event
    (the node's prior state is captured so the upsert can be compensated).
    """
    b = _bundle(ontology)
    node = service.upsert_node(
        b.backend, b.events, id=id, kind=kind, name=name, labels=labels, properties=properties, actor=actor
    )
    return _node_to_dict(node)  # type: ignore[return-value]


@mcp.tool()
def kg_node_set_label(id: str, label: str, actor: str = "anonymous", ontology: str | None = None) -> dict | None:
    """Add a label to an existing node. Logged + reversible."""
    b = _bundle(ontology)
    return _node_to_dict(service.set_label(b.backend, b.events, id, label, actor=actor))


@mcp.tool()
def kg_node_set_property(
    id: str, key: str, value: Any, actor: str = "anonymous", ontology: str | None = None
) -> dict | None:
    """Set a single property on a node. Value must be JSON-serializable. Logged + reversible."""
    b = _bundle(ontology)
    return _node_to_dict(service.set_property(b.backend, b.events, id, key, value, actor=actor))


@mcp.tool()
def kg_node_delete(id: str, actor: str = "anonymous", ontology: str | None = None) -> dict:
    """Delete a node (cascade removes its edges/properties).

    Captures the full node + its incident edges in the event so the delete can
    be compensated (restored) later.
    """
    b = _bundle(ontology)
    return service.delete_node(b.backend, b.events, id, actor=actor)


@mcp.tool()
def kg_edge_add(
    from_id: str,
    to_id: str,
    type: str,
    properties: dict[str, Any] | None = None,
    actor: str = "anonymous",
    ontology: str | None = None,
) -> dict:
    """Add an edge. Triples (from, type, to) are unique; repeats update properties."""
    b = _bundle(ontology)
    edge = service.add_edge(b.backend, b.events, from_id, to_id, type, properties=properties, actor=actor)
    return _edge_to_dict(edge)


@mcp.tool()
def kg_edge_remove(
    from_id: str, to_id: str, type: str, actor: str = "anonymous", ontology: str | None = None
) -> dict:
    """Remove a specific edge by (from, type, to). Idempotent. Logged + reversible."""
    b = _bundle(ontology)
    return service.remove_edge(b.backend, b.events, from_id, to_id, type, actor=actor)


# ---- event log: read + reversal + replay ----------------------------


@mcp.tool()
def kg_events_tail(n: int = 20, ontology: str | None = None) -> list[dict]:
    """The most recent events from the append-only log."""
    return [e.to_dict() for e in _bundle(ontology).events.tail(n)]


@mcp.tool()
def kg_event_revert(event_id: str, actor: str = "operator", ontology: str | None = None) -> dict:
    """Reverse an event by emitting a compensating event. The original is never deleted."""
    return service.revert_event(_bundle(ontology).events, event_id, actor=actor).to_dict()


@mcp.tool()
def kg_replay(upto_ts: str | None = None, ontology: str | None = None) -> dict:
    """Rebuild the graph projection from the event log.

    With upto_ts (ISO 8601) the graph is projected to that point in time. The
    event log is never cleared.
    """
    b = _bundle(ontology)
    return service.replay_log(b.backend, b.events, upto_ts=upto_ts)


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
