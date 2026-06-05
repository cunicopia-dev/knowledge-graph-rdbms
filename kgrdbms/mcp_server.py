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
    kg_schema             — observed vocabulary (kinds, edge types, labels, keys);
                            read this FIRST to query without guessing
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
    kg_import             — bulk {nodes, edges} in ONE call (one batch; use this
                            to compose an ontology instead of N upsert calls)

  federation (read across many ontologies at once — multithreaded fan-out)
    kg_federated_schema       — union vocabulary across ontologies
    kg_federated_stats        — node/edge totals across ontologies
    kg_federated_nodes_by_kind/_by_label — nodes across ontologies, tagged by source
    kg_federated_node         — find an id across the federation (identity-aware)
    kg_identity               — materialized SAME_AS cluster of a node

  backbone (cross-ontology links + the prefix/IRI registry)
    kg_link / kg_same_as      — relate nodes that live in DIFFERENT ontologies
    kg_links_of               — cross-ontology links touching a node
    kg_prefix_add / kg_prefixes / kg_expand — CURIE prefix -> IRI registry

  rdf boundary
    kg_rdf_export         — serialize an ontology to Turtle/N-Triples (RDF-star)
    kg_rdf_import         — load RDF text back in (gated + logged, replayable)

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

from kgrdbms import backbone, rdf, resolver, service
from kgrdbms.federation import FederatedNode, Federation, Located
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
        "kg_ontology_create to add one. When working with an ontology whose "
        "contents you don't already know, call kg_schema FIRST — it returns the "
        "exact kinds, edge types, labels, and property keys so you can query by "
        "real values instead of guessing. Writes are gated by compiled-in "
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


def _located_to_dict(l: Located) -> dict:
    return {"ontology": l.ontology, "node": _node_to_dict(l.node)}


def _federation(ontologies: list[str] | None) -> Federation:
    """A Federation over the named ontologies, or every registered one."""
    return Federation(list(ontologies)) if ontologies else Federation.all()


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
    location: str | None = None,
) -> dict:
    """Register a new ontology (or update an existing one's metadata).

    `backend` routes the engine ("sqlite" and "postgres" are live; "neo4j" is a
    registered stub). `location` is the backend location — a DSN
    (postgresql://…) for postgres; omit for a managed sqlite file. `stance` is
    the ontology's own extraction opinion ("literal" vs "inferential") that a
    composing agent should honor. The ontology is immediately addressable via
    the `ontology` argument on any tool.
    """
    entry = resolver.register(
        name, backend=backend, description=description, stance=stance, path=location
    )
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
def kg_schema(samples: bool = False, ontology: str | None = None) -> dict:
    """The observed schema of an ontology — CALL THIS FIRST when you don't already
    know what an ontology contains, before kg_nodes_by_kind / kg_nodes_by_label /
    kg_node_get. It tells you the exact vocabulary so you never have to guess.

    Returns:
      - kinds            — every node `kind` and its count
      - edge_types       — every edge `type` and its count
      - labels           — every label and its count
      - node_keys_by_kind— for each kind, which property keys its nodes carry (+counts)
      - edge_keys        — property keys that appear on edges

    With samples=True, also returns per kind a few example node ids (showing the
    id/CURIE convention) and, for enum-like properties, the set of distinct values
    a key takes (free-text keys are left un-enumerated). Read-only; cheap.
    """
    return _bundle(ontology).backend.schema(samples=samples)


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


@mcp.tool()
def kg_import(
    nodes: list[dict] | None = None,
    edges: list[dict] | None = None,
    actor: str = "kg-compose",
    ontology: str | None = None,
) -> dict:
    """Bulk-create many nodes and edges in ONE call (one gated + logged transaction).

    Use this instead of dozens of kg_node_upsert / kg_edge_add calls when
    composing an ontology from a document — pass the whole graph at once:

        nodes = [{"id": "person:ada", "kind": "Person", "name": "Ada Lovelace",
                  "labels": ["Person"], "properties": {"born": 1815}}, ...]
        edges = [{"from": "person:ada", "to": "field:cs", "type": "FOUNDED",
                  "properties": {"year": 1843}}, ...]   # from_node/to_node also accepted

    Every node and edge is still individually gated (invariants then policy) and
    recorded as a reversible event — bulk does not bypass the gate. The batch
    commits once and is atomic: if any item is denied, nothing is written.
    Returns {ontology, nodes_imported, edges_imported}.
    """
    b = _bundle(ontology)
    res = service.import_graph(b.backend, b.events, nodes=nodes, edges=edges, actor=actor)
    return {"ontology": b.entry.name, **res}


# ---- RDF boundary: export / import ----------------------------------


@mcp.tool()
def kg_rdf_export(
    format: str = "turtle",
    edge_strategy: str = "rdf-star",
    ontology: str | None = None,
) -> dict:
    """Serialize an ontology to RDF text (dependency-free).

    Hand the result to any RDF store (Stardog, rdflib, Jena) to run SPARQL —
    the LPG stays the store of record; RDF is just the boundary format.

      format        — "turtle" (human-readable, default) or "ntriples" (lossless)
      edge_strategy — how edge properties cross: "rdf-star" (quoted triples,
                      default), "reification" (rdf:Statement), or "lossy"
                      (bare triples; dropped count returned, never silent)

    Node ids round-trip as CURIEs (person:ada <-> <https://kg.local/person/ada>).
    Returns {ontology, format, triples, dropped_edge_props, rdf}.
    """
    b = _bundle(ontology)
    ctx = rdf.IriContext(edge_strategy=edge_strategy)
    triples = rdf.export_graph(b.backend, ctx)
    text = rdf.to_turtle(triples, ctx) if format in ("turtle", "ttl") else rdf.to_ntriples(triples)
    return {
        "ontology": b.entry.name,
        "format": format,
        "triples": len(triples),
        "dropped_edge_props": getattr(triples, "dropped_edge_props", 0),
        "rdf": text,
    }


@mcp.tool()
def kg_rdf_import(
    text: str,
    format: str = "ntriples",
    edge_strategy: str = "rdf-star",
    actor: str = "rdf-import",
    ontology: str | None = None,
) -> dict:
    """Load RDF text into an ontology through the gated + logged path (replayable).

    N-Triples import is dependency-free; Turtle import needs the [rdf] extra
    (rdflib). `edge_strategy` must match how the RDF encoded its edges, so the
    edge properties decode back onto the right edges. Returns
    {ontology, nodes_imported, edges_imported}.
    """
    b = _bundle(ontology)
    ctx = rdf.IriContext(edge_strategy=edge_strategy)
    res = rdf.import_rdf(b.backend, b.events, text, fmt=format, ctx=ctx, actor=actor)
    return {"ontology": b.entry.name, **res}


# =====================================================================
# FEDERATION — read across many ontologies at once (multithreaded fan-out)
# =====================================================================
#
# Each ontology is a separate file; a federated read fans out across them
# concurrently and merges the results tagged by source. `ontologies` selects
# members (omit for every registered ontology). Reads only — cross-ontology
# writes are the backbone tools below.


@mcp.tool()
def kg_federated_schema(samples: bool = False, ontologies: list[str] | None = None) -> dict:
    """Union schema across many ontologies — every kind, edge type, label, and
    property-key (summed) plus each member's own schema. The cross-ontology
    analogue of kg_schema; call it to see your whole world's vocabulary at once."""
    return _federation(ontologies).schema(samples=samples)


@mcp.tool()
def kg_federated_stats(ontologies: list[str] | None = None) -> dict:
    """Node/edge totals across the federation, with a per-ontology breakdown."""
    return _federation(ontologies).stats()


@mcp.tool()
def kg_federated_nodes_by_kind(kind: str, ontologies: list[str] | None = None) -> list[dict]:
    """All nodes of a kind across every ontology, each tagged with its source
    ontology ({ontology, node})."""
    return [_located_to_dict(l) for l in _federation(ontologies).nodes_by_kind(kind)]


@mcp.tool()
def kg_federated_nodes_by_label(label: str, ontologies: list[str] | None = None) -> list[dict]:
    """All nodes carrying a label across every ontology, tagged by source."""
    return [_located_to_dict(l) for l in _federation(ontologies).nodes_by_label(label)]


@mcp.tool()
def kg_federated_node(id: str, ontologies: list[str] | None = None) -> dict:
    """Find a node id across the federation (identity-aware).

    Returns every occurrence tagged by ontology. Copies in `shared_identity`
    ontologies are merged into one `merged` entity (labels unioned, properties
    merged); local-identity copies stay separate. Returns
    {id, occurrences, shared, merged}.
    """
    fn: FederatedNode = _federation(ontologies).node(id)
    return {
        "id": fn.id,
        "occurrences": [_located_to_dict(l) for l in fn.occurrences],
        "shared": fn.shared,
        "merged": _node_to_dict(fn.merged),
    }


@mcp.tool()
def kg_identity(ontology: str, id: str, ontologies: list[str] | None = None) -> list[dict]:
    """The materialized SAME_AS cluster of a node — every leaf node explicitly
    asserted (transitively) to be the same real-world entity, fetched live and
    tagged by ontology. Pairs with kg_same_as / kg_link."""
    return [_located_to_dict(l) for l in _federation(ontologies).identity(ontology, id)]


# =====================================================================
# BACKBONE — cross-ontology links + the prefix/IRI registry
# =====================================================================
#
# A leaf edge can't cross ontologies (its foreign key is within one file); a
# backbone link can. Links live in the index graph as Ref proxy nodes joined by
# edges, written through the same gated + logged path. The prefix registry is
# the lightweight identity backbone (CURIE prefix -> IRI base).


@mcp.tool()
def kg_link(
    from_ontology: str,
    from_id: str,
    type: str,
    to_ontology: str,
    to_id: str,
    properties: dict[str, Any] | None = None,
    symmetric: bool = False,
    actor: str = "backbone",
) -> dict:
    """Assert a typed relationship between nodes in two DIFFERENT ontologies
    (impossible as a normal edge — those can't cross files). symmetric=True also
    writes the reverse. Gated + logged in the index's event log."""
    return backbone.link(from_ontology, from_id, type, to_ontology, to_id,
                         properties=properties, symmetric=symmetric, actor=actor)


@mcp.tool()
def kg_same_as(
    from_ontology: str, from_id: str, to_ontology: str, to_id: str, actor: str = "backbone"
) -> dict:
    """Assert two leaf nodes in different ontologies are the same real-world
    entity (a symmetric SAME_AS link). Read the cluster back with kg_identity."""
    return backbone.same_as(from_ontology, from_id, to_ontology, to_id, actor=actor)


@mcp.tool()
def kg_links_of(ontology: str, id: str, type: str | None = None) -> list[dict]:
    """Every cross-ontology link touching a node (both directions)."""
    return [
        {"direction": l.direction, "type": l.type, "ontology": l.other_ontology,
         "id": l.other_id, "properties": l.properties}
        for l in backbone.links_of(ontology, id, type=type)
    ]


@mcp.tool()
def kg_prefix_add(prefix: str, iri_base: str, actor: str = "backbone") -> dict:
    """Bind a CURIE prefix to an IRI base (e.g. person -> https://kg.local/person/)."""
    return backbone.register_prefix(prefix, iri_base, actor=actor)


@mcp.tool()
def kg_prefixes() -> dict:
    """All registered prefix -> IRI-base bindings."""
    return backbone.prefixes()


@mcp.tool()
def kg_expand(curie: str) -> dict:
    """Expand a CURIE to its full IRI via the prefix registry ({curie, iri})."""
    return {"curie": curie, "iri": backbone.expand(curie)}


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
