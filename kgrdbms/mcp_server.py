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
    kg_ontology_delete    — deregister an ontology (purge=True also deletes data)

  reads (each takes optional `ontologies=[...]` to fan out across many — one
         multithreaded read instead of a separate federated tool family)
    kg_schema             — observed vocabulary + totals; read this FIRST to
                            query without guessing
    kg_node_get           — fetch a node by id (identity-aware across `ontologies`)
    kg_find               — nodes by kind and/or label, tagged by ontology
    kg_edges              — edges of a node (direction = out | in | both)
    kg_neighborhood       — undirected BFS within depth
    kg_shortest_path      — shortest path between two ids
    kg_descendants        — recursive walk along one edge type

  writes (gated: invariants.enforce THEN policy.mutation_check)
    kg_node_upsert        — create/update a node (also adds labels / sets properties)
    kg_node_delete        — delete a node and its edges
    kg_edge_add           — add a (from, type, to) edge
    kg_edge_remove        — remove a (from, type, to) edge
    kg_import             — bulk {nodes, edges} in ONE call (one batch; use this
                            to compose an ontology instead of N upsert calls)

  cross-ontology (the backbone; federated reads fold into the reads above)
    kg_link               — relate nodes in DIFFERENT ontologies (SAME_AS too)
    kg_links_of           — cross-ontology links touching a node
    kg_identity           — materialized SAME_AS cluster of a node
    kg_prefix_add         — bind a CURIE prefix to an IRI base
    kg_prefix_resolve     — expand a CURIE, or list all prefix bindings

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

import hmac
import os
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError as exc:  # pragma: no cover - exercised via the CLI
    raise SystemExit(
        "The MCP server needs the optional 'mcp' dependency, which isn't installed.\n"
        "Install the extra:\n"
        "    pip install 'knowledge-graph-rdbms[mcp]'\n"
        "or run it directly with:\n"
        "    uvx --from 'knowledge-graph-rdbms[mcp]' kgrdbms-mcp"
    ) from exc

from kgrdbms import backbone, rdf, resolver, service, virtual
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


@mcp.tool()
def kg_ontology_delete(name: str, purge: bool = False) -> dict:
    """Remove an ontology from the registry (inverse of kg_ontology_create).

    By default only deregisters — the data file is left on disk and the ontology
    can be recreated intact. purge=True also deletes the on-disk SQLite file
    (destructive, irreversible). External (postgres) databases are never dropped
    from here. Returns {name, deregistered, purged}.
    """
    res = resolver.unregister(name, purge=purge)
    _BUNDLES.pop(name, None)  # drop any cached bundle for the removed ontology
    return res


# =====================================================================
# READS
# =====================================================================


@mcp.tool()
def kg_schema(
    samples: bool = False, ontology: str | None = None, ontologies: list[str] | None = None
) -> dict:
    """The observed schema — CALL THIS FIRST when you don't know what an ontology
    contains, before kg_find / kg_node_get. Returns the exact vocabulary so you
    never guess: kinds, edge types, labels, property keys per kind (+counts), and
    node/edge totals.

    Scope: omit `ontologies` (optionally set a single `ontology`) for one graph.
    Pass `ontologies=[...]` to fan out across many at once (multithreaded) — you
    get a unioned `merged` schema plus each member's own under `by_ontology`.
    samples=True adds example ids + enum-like property values per kind.
    """
    if ontologies:
        return _federation(ontologies).schema(samples=samples)
    return _bundle(ontology).backend.schema(samples=samples)


@mcp.tool()
def kg_node_get(
    id: str, ontology: str | None = None, ontologies: list[str] | None = None
) -> dict | None:
    """Fetch a node by id. One graph: returns the node, or null if absent.

    Pass `ontologies=[...]` to look the id up across the federation, identity-aware:
    returns {id, occurrences:[{ontology, node}], shared, merged} where copies in
    shared-identity ontologies are merged into one `merged` entity.
    """
    if ontologies:
        fn: FederatedNode = _federation(ontologies).node(id)
        return {
            "id": fn.id,
            "occurrences": [_located_to_dict(l) for l in fn.occurrences],
            "shared": fn.shared,
            "merged": _node_to_dict(fn.merged),
        }
    return _node_to_dict(_bundle(ontology).backend.node(id))


@mcp.tool()
def kg_find(
    kind: str | None = None,
    label: str | None = None,
    ontology: str | None = None,
    ontologies: list[str] | None = None,
) -> list[dict]:
    """Find nodes by `kind` and/or `label`, each tagged with its source ontology
    ([{ontology, node}]). Give a kind, a label, or both (both = nodes of that kind
    that also carry that label).

    Scope: omit `ontologies` for a single graph; pass `ontologies=[...]` to search
    across many at once (multithreaded fan-out). Replaces the old by-kind/by-label
    and federated variants.
    """
    if kind is None and label is None:
        raise ValueError("kg_find needs a kind and/or a label")
    if ontologies:
        fed = _federation(ontologies)
        located = fed.nodes_by_kind(kind) if kind is not None else fed.nodes_by_label(label)
        if kind is not None and label is not None:
            located = [l for l in located if label in l.node.labels]
        return [_located_to_dict(l) for l in located]
    b = _bundle(ontology)
    nodes = b.backend.nodes_by_kind(kind) if kind is not None else b.backend.nodes_by_label(label)
    if kind is not None and label is not None:
        nodes = [n for n in nodes if label in n.labels]
    return [{"ontology": b.entry.name, "node": _node_to_dict(n)} for n in nodes]


@mcp.tool()
def kg_edges(
    id: str, direction: str = "out", edge_type: str | None = None, ontology: str | None = None
) -> list[dict]:
    """Edges of a node, optionally filtered by `edge_type`. `direction` is "out"
    (default), "in", or "both". Each result is the edge plus the node on the other
    end: {direction, id, from, to, type, properties, node}.

    Stored edges and *virtual* edges (resolved live from an external SQL source —
    see kg_virtual_edge_add) are unioned transparently; a virtual edge carries
    `"_virtual": true` in its properties."""
    b = _bundle(ontology).backend
    out: list[dict] = []
    if direction in ("out", "both"):
        for edge, target in b.out(id, edge_type):
            d = _edge_to_dict(edge)
            d["direction"], d["node"] = "out", _node_to_dict(target)
            out.append(d)
    if direction in ("in", "both"):
        for edge, source in b.in_(id, edge_type):
            d = _edge_to_dict(edge)
            d["direction"], d["node"] = "in", _node_to_dict(source)
            out.append(d)
    for d_dir, edge, far in virtual.augment(b, id, direction, edge_type):
        d = _edge_to_dict(edge)
        d["direction"], d["node"] = d_dir, _node_to_dict(far)
        out.append(d)
    return out


# =====================================================================
# VIRTUAL EDGES — relationships resolved live from an external SQL source
# =====================================================================


@mcp.tool()
def kg_virtual_edge_add(
    edge_type: str,
    query: str,
    dsn: str | None = None,
    dsn_env: str | None = None,
    source: str = "id",
    target_col: str = "to_id",
    target_id_template: str = "{value}",
    target_kind: str = "external",
    name_col: str | None = None,
    prop_cols: list[str] | None = None,
    directions: str = "out",
    source_type: str = "sql",
    catalog: dict | None = None,
    table: str | None = None,
    tables: list[str] | None = None,
    snapshot_id: int | None = None,
    ontology: str | None = None,
) -> dict:
    """Bind an edge TYPE to a query that resolves its instances live from an
    external source — no rows are copied into the graph (Ontology-Based Data
    Access). At traversal time kg_edges runs `query`, parameterized by the node
    you're standing on, and synthesizes the edges; results carry `_virtual: true`.

    `query` must hold exactly one placeholder bound to the source value — the
    driver's native marker ('?' for sqlite *and* iceberg/duckdb, '%s' for
    postgres). The value is always *bound*, never string-formatted, so the query
    is injection-safe.

    Source of the bound value (`source`): "id" (default), "id_slug" (after the
    last ':'), or "prop:<key>" (a node property, e.g. "prop:ticker"). Each result
    row yields one edge: `target_col` is the far-end value, `target_id_template`
    (e.g. "company:{value}") builds its node id, `name_col` names it, `prop_cols`
    (or every other column) become edge properties. `directions` is out|in|both.

    SOURCE_TYPE selects where `query` runs:
      * "sql" (default) — a direct SQL store. Prefer `dsn_env` (an env-var *name*,
        resolved at query time) so secrets stay out of the graph; `dsn` is the
        literal form for non-secret paths.
      * "iceberg" — an Apache Iceberg table in a lakehouse. Give `catalog` (a
        pyiceberg catalog property dict, e.g. {"name","type","uri","warehouse"};
        any value written "env:VAR" is resolved from the environment so creds stay
        out of the graph) and `table` ("namespace.table"). pyiceberg resolves the
        current metadata (or `snapshot_id` for a time-travel read); DuckDB scans it.
        Write `query` against the table's leaf name, e.g.
        "SELECT b AS to_id FROM co_held WHERE a = ?". To JOIN across tables in one
        edge, pass `tables` (a list of "namespace.table"); each is mounted as a
        view by its leaf name and the query may join them freely (`snapshot_id`
        then doesn't apply — it pins a single table's version).

    The binding is stored as a reserved `_VirtualEdge` node in the ontology.
    """
    ve = virtual.VirtualEdge(
        edge_type=edge_type, query=query, dsn=dsn, dsn_env=dsn_env, source=source,
        target_col=target_col, target_id_template=target_id_template,
        target_kind=target_kind, name_col=name_col, prop_cols=list(prop_cols or []),
        directions=directions, source_type=source_type, catalog=catalog,
        table=table, tables=list(tables or []), snapshot_id=snapshot_id,
    )
    virtual.register(_bundle(ontology).backend, ve)
    return {
        "edge_type": edge_type, "directions": directions,
        "source_type": source_type, "bound": True,
    }


@mcp.tool()
def kg_virtual_edges_list(ontology: str | None = None) -> list[dict]:
    """List the ontology's virtual-edge bindings (edge type, source, directions,
    target shape, and the query). The connection string is shown by reference
    (`dsn_env`) or literal (`dsn`) exactly as stored."""
    return [
        {
            "edge_type": ve.edge_type, "directions": ve.directions, "source": ve.source,
            "target_kind": ve.target_kind, "target_id_template": ve.target_id_template,
            "source_type": ve.source_type, "dsn_env": ve.dsn_env, "dsn": ve.dsn,
            "catalog": ve.catalog, "table": ve.table, "tables": list(ve.tables),
            "snapshot_id": ve.snapshot_id, "query": ve.query,
        }
        for ve in virtual.list_bindings(_bundle(ontology).backend)
    ]


@mcp.tool()
def kg_virtual_edge_remove(edge_type: str, ontology: str | None = None) -> dict:
    """Remove a virtual-edge binding by type. The external source is untouched."""
    removed = virtual.unregister(_bundle(ontology).backend, edge_type)
    return {"edge_type": edge_type, "removed": removed}


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
    """Create or update a node — also the way to add labels or set properties
    (pass `labels=[...]` / `properties={...}`; both merge into the existing node).
    Properties are JSON-serializable.

    Gated by invariants then policy. Logged as a reversible NODE_UPSERT event
    (the node's prior state is captured so the upsert can be compensated).
    """
    b = _bundle(ontology)
    node = service.upsert_node(
        b.backend, b.events, id=id, kind=kind, name=name, labels=labels, properties=properties, actor=actor
    )
    return _node_to_dict(node)  # type: ignore[return-value]


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
# CROSS-ONTOLOGY — the backbone (links + identity + the prefix registry)
# =====================================================================
#
# Federated *reads* fold into the read tools above via their `ontologies=[...]`
# parameter (kg_schema, kg_find, kg_node_get) — there is no separate federated
# tool family. What remains here is the cross-ontology *write* surface and the
# reads unique to it: a leaf edge can't cross files, so cross-ontology links live
# in the index graph as Ref proxy nodes joined by edges, written through the same
# gated + logged path. The prefix registry is the lightweight identity backbone.


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
    (impossible as a normal edge — those can't cross files). For "same real-world
    entity", use type="SAME_AS" with symmetric=True (read the cluster back with
    kg_identity). symmetric=True also writes the reverse. Gated + logged."""
    return backbone.link(from_ontology, from_id, type, to_ontology, to_id,
                         properties=properties, symmetric=symmetric, actor=actor)


@mcp.tool()
def kg_links_of(ontology: str, id: str, type: str | None = None) -> list[dict]:
    """Every cross-ontology link touching a node (both directions)."""
    return [
        {"direction": l.direction, "type": l.type, "ontology": l.other_ontology,
         "id": l.other_id, "properties": l.properties}
        for l in backbone.links_of(ontology, id, type=type)
    ]


@mcp.tool()
def kg_identity(ontology: str, id: str) -> list[dict]:
    """The materialized SAME_AS cluster of a node — every leaf node explicitly
    asserted (transitively) to be the same real-world entity, fetched live and
    tagged by ontology ([{ontology, node}]). Pairs with kg_link(type=SAME_AS)."""
    return [_located_to_dict(l) for l in Federation.all().identity(ontology, id)]


@mcp.tool()
def kg_prefix_add(prefix: str, iri_base: str, actor: str = "backbone") -> dict:
    """Bind a CURIE prefix to an IRI base (e.g. person -> https://kg.local/person/)."""
    return backbone.register_prefix(prefix, iri_base, actor=actor)


@mcp.tool()
def kg_prefix_resolve(curie: str | None = None) -> dict:
    """Resolve the prefix/IRI registry. With `curie`, expand it to its full IRI
    ({curie, iri}); without, return every prefix -> IRI-base binding."""
    if curie is None:
        return backbone.prefixes()
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


TOKEN_ENV = "KGRDBMS_MCP_TOKEN"


def _require_bearer(app, token: str):
    """Wrap an ASGI app so HTTP requests must carry `Authorization: Bearer <token>`.

    Pure ASGI — no new dependency (starlette/uvicorn already ride in with the
    `[mcp]` extra, and the comparison is stdlib `hmac`). Only HTTP scopes are
    guarded; lifespan/other scopes pass straight through. The token is compared
    in constant time so a wrong guess leaks no timing signal.
    """
    expected = f"Bearer {token}"

    async def guarded(scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            presented = headers.get(b"authorization", b"").decode("latin-1")
            if not hmac.compare_digest(presented, expected):
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b"Bearer"),
                    ],
                })
                await send({
                    "type": "http.response.body",
                    "body": b'{"error":"unauthorized"}',
                })
                return
        await app(scope, receive, send)

    return guarded


def _configure_host_allowlist(host: str, allow_hosts: list[str]) -> None:
    """Set the transport's DNS-rebinding Host allowlist for an HTTP bind.

    FastMCP auto-enables DNS-rebinding protection with a *localhost-only* Host
    allowlist (it's constructed with the default 127.0.0.1 bind). Once you bind
    elsewhere, that baked-in allowlist would reject every real request (HTTP
    421), so we rebuild it for the actual bind.

    Secure by default: protection stays on; the bind host and localhost are
    auto-allowed. Pass extra hostnames clients use (e.g. a Tailscale MagicDNS
    name) via ``allow_hosts``; the ``host:*`` wildcard matches any port. The
    sentinel ``"*"`` disables Host checking entirely — for when a fronting proxy
    already validates it.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    if "*" in allow_hosts:
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        )
        return

    hosts = [f"{host}:*", "127.0.0.1:*", "localhost:*", "[::1]:*", *allow_hosts]
    origins = [f"http://{h}" for h in hosts] + [f"https://{h}" for h in hosts]
    mcp.settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def serve(
    transport: str = "stdio",
    host: str | None = None,
    port: int | None = None,
    allow_hosts: list[str] | None = None,
) -> None:
    """Run the MCP server.

    Transports: stdio (default; a private pipe to one local client), sse, and
    streamable-http. The HTTP transports bind to ``host``/``port`` (defaults:
    127.0.0.1 — localhost only; widening the bind is always a deliberate opt-in).

    When the ``KGRDBMS_MCP_TOKEN`` env var is set, the HTTP transports require an
    ``Authorization: Bearer <token>`` header — so the graph can be served over a
    wire (e.g. a private mesh like Tailscale) with the connection authenticated.
    stdio is unaffected. With no token set, behavior is unchanged.

    ``allow_hosts`` extends the DNS-rebinding Host allowlist with the hostnames
    clients connect by (the bind host and localhost are always allowed); ``"*"``
    disables Host checking for proxy-fronted setups. See
    :func:`_configure_host_allowlist`.
    """
    if host is not None:
        mcp.settings.host = host
    if port is not None:
        mcp.settings.port = port

    if transport in ("streamable-http", "sse"):
        _configure_host_allowlist(mcp.settings.host, list(allow_hosts or []))

    token = os.environ.get(TOKEN_ENV)
    if transport in ("streamable-http", "sse") and token:
        import uvicorn  # provided by the [mcp] extra; no new dependency

        app = mcp.streamable_http_app() if transport == "streamable-http" else mcp.sse_app()
        uvicorn.run(
            _require_bearer(app, token),
            host=mcp.settings.host,
            port=mcp.settings.port,
        )
        return

    mcp.run(transport=transport)


def main() -> None:  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(description="MCP server over the kgrdbms graph")
    parser.add_argument(
        "--transport", default="stdio", choices=["stdio", "sse", "streamable-http"]
    )
    parser.add_argument("--host", help="bind address for HTTP transports (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, help="bind port for HTTP transports (default: 8000)")
    parser.add_argument(
        "--allow-host",
        action="append",
        dest="allow_hosts",
        metavar="HOST",
        help="extra Host header value clients connect by, e.g. 'name.example:*' "
        "(repeatable; bind host + localhost are always allowed; '*' disables checking)",
    )
    args = parser.parse_args()
    serve(transport=args.transport, host=args.host, port=args.port,
          allow_hosts=args.allow_hosts)


if __name__ == "__main__":  # pragma: no cover
    main()
