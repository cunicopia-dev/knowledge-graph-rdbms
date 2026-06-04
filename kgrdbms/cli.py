"""`kg` — a command-line interface to the graph.

Built on the standard library only (argparse + json), so installing the CLI
adds no third-party dependencies.

Reads talk to the Graph directly. Writes go through `kgrdbms.service`, which
means every mutation passes the invariants + policy gate and is recorded to
the append-only event log — exactly like the MCP server. So `kg replay` and
`kg revert` keep working, and a custom policy is honored at the console too.

Targeting a graph (two ways, same as the MCP server's `ontology` argument):
  * --ontology NAME routes through the resolver (named, registered, multi-engine).
    Omit it and you hit the default ontology (the legacy $KGRDBMS_HOME/graph.db).
  * --db PATH is the raw escape hatch: open that exact file directly, no registry.

Storage root: ~/.kgrdbms, or set KGRDBMS_HOME. `kg ontology list/create` manage
the registry. Add --json to any command for machine-readable output.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from kgrdbms import __version__, rdf, resolver, service
from kgrdbms.events import EventLog
from kgrdbms.graph import Edge, Graph, Node
from kgrdbms.invariants import InvariantViolation


# ---- value parsing (a real UX decision — see note in the CLI summary) ----


def _parse_prop_value(raw: str) -> Any:
    """Interpret a property value supplied as a shell string.

    Default: try to parse it as JSON, so `--prop n=42` stores the int 42,
    `--prop ok=true` stores the bool True, `--prop tags=["a","b"]` stores a
    list — and anything that isn't valid JSON (e.g. `--prop name=Ada`) is kept
    as a plain string.
    """
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _parse_props(pairs: list[str] | None) -> dict[str, Any]:
    """Turn ['k=v', ...] into a properties dict, with values parsed."""
    out: dict[str, Any] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"error: --prop must be key=value, got {pair!r}")
        key, _, raw = pair.partition("=")
        out[key] = _parse_prop_value(raw)
    return out


# ---- serialization / formatting --------------------------------------


def _node_dict(n: Node | None) -> dict | None:
    if n is None:
        return None
    return {"id": n.id, "kind": n.kind, "name": n.name,
            "labels": sorted(n.labels), "properties": n.properties}


def _edge_dict(e: Edge) -> dict:
    return {"id": e.id, "from": e.from_node, "to": e.to_node,
            "type": e.type, "properties": e.properties}


def _fmt_node(n: Node) -> str:
    labels = (" [" + ", ".join(sorted(n.labels)) + "]") if n.labels else ""
    props = ("  " + json.dumps(n.properties)) if n.properties else ""
    return f"{n.id}  ({n.kind}) {n.name}{labels}{props}"


# ---- app context -----------------------------------------------------


class App:
    """Holds the graph/log target and output mode for a single run.

    Opening is lazy: registry-only commands (`ontology list`) never touch a
    graph. `--db PATH` opens that exact file directly (escape hatch); otherwise
    the resolver routes `--ontology NAME` (or the default) to its backend + log.
    """

    def __init__(self, db: str | None, ontology: str | None, as_json: bool) -> None:
        self._db = db
        self._ontology = ontology
        self.as_json = as_json
        self._graph: Any = None
        self._events: EventLog | None = None
        self._db_path: str | None = None
        self._ont_name: str | None = None

    def _ensure(self) -> None:
        if self._graph is not None:
            return
        if self._db:  # raw escape hatch: exact file, no registry
            self._graph = Graph(path=self._db)
            self._db_path = self._db
        else:  # route a name (or the default) through the control plane
            resolved = resolver.resolve(self._ontology)
            self._graph = resolved.backend
            self._events = resolved.events
            self._db_path = resolved.entry.path
            self._ont_name = resolved.entry.name

    @property
    def graph(self) -> Any:
        self._ensure()
        return self._graph

    @property
    def events(self) -> EventLog:
        self._ensure()
        if self._events is None:
            self._events = EventLog(self._graph)
        return self._events

    @property
    def db_path(self) -> str:
        self._ensure()
        return self._db_path  # type: ignore[return-value]

    @property
    def ontology(self) -> str | None:
        self._ensure()
        return self._ont_name

    def close(self) -> None:
        if self._graph is not None:
            self._graph.close()

    def emit(self, obj: Any, human: str | None = None) -> None:
        """Print a result: JSON when --json, otherwise the human rendering."""
        if self.as_json:
            print(json.dumps(obj, indent=2, default=str))
        elif human is not None:
            print(human)
        else:
            print(json.dumps(obj, default=str))


# ---- read handlers ---------------------------------------------------


def cmd_stats(app: App, args) -> int:
    res = {
        "ontology": app.ontology,
        "nodes_total": app.graph.total_nodes(),
        "edges_total": app.graph.total_edges(),
        "nodes_by_kind": app.graph.count_nodes_by_kind(),
        "edges_by_type": app.graph.count_edges_by_type(),
        "db_path": app.db_path,
    }
    ont = f"ontology: {res['ontology']}\n" if res["ontology"] else ""
    human = (
        f"{ont}"
        f"db: {res['db_path']}\n"
        f"nodes: {res['nodes_total']:,}   edges: {res['edges_total']:,}\n"
        f"by kind: {res['nodes_by_kind'] or '{}'}\n"
        f"by type: {res['edges_by_type'] or '{}'}"
    )
    app.emit(res, human)
    return 0


# ---- registry handlers (the control plane / db-of-dbs) ---------------


def _ontology_dict(e) -> dict:
    return {"name": e.name, "backend": e.backend, "stance": e.stance,
            "description": e.description, "path": e.path}


def cmd_ontology_list(app: App, args) -> int:
    ents = resolver.list_ontologies()
    human = "\n".join(
        f"{e.name:16} {e.backend:8} {e.stance:12} {e.path}" for e in ents
    ) or "(no ontologies registered yet)"
    app.emit([_ontology_dict(e) for e in ents], human)
    return 0


def cmd_ontology_create(app: App, args) -> int:
    entry = resolver.register(
        args.name, backend=args.backend, description=args.description or "", stance=args.stance,
        path=args.location,
    )
    app.emit(
        _ontology_dict(entry),
        f"registered ontology {entry.name!r} (backend={entry.backend}, stance={entry.stance})\n{entry.path}",
    )
    return 0


def cmd_node_get(app: App, args) -> int:
    n = app.graph.node(args.id)
    if n is None:
        print(f"no node {args.id!r}", file=sys.stderr)
        app.emit(None, "")
        return 1
    app.emit(_node_dict(n), _fmt_node(n))
    return 0


def _emit_nodes(app: App, nodes: list[Node]) -> int:
    app.emit([_node_dict(n) for n in nodes],
             "\n".join(_fmt_node(n) for n in nodes) or "(none)")
    return 0


def cmd_nodes_by_kind(app: App, args) -> int:
    return _emit_nodes(app, app.graph.nodes_by_kind(args.kind))


def cmd_nodes_by_label(app: App, args) -> int:
    return _emit_nodes(app, app.graph.nodes_by_label(args.label))


def _emit_edges(app: App, pairs, role: str) -> int:
    rows = []
    human = []
    for edge, other in pairs:
        d = _edge_dict(edge)
        d[role] = _node_dict(other)
        rows.append(d)
        arrow = f"-[{edge.type}]->" if role == "target" else f"<-[{edge.type}]-"
        human.append(f"{arrow}  {_fmt_node(other)}")
    app.emit(rows, "\n".join(human) or "(none)")
    return 0


def cmd_out(app: App, args) -> int:
    return _emit_edges(app, app.graph.out(args.id, args.type), "target")


def cmd_in(app: App, args) -> int:
    return _emit_edges(app, app.graph.in_(args.id, args.type), "source")


def cmd_path(app: App, args) -> int:
    path = app.graph.shortest_path(args.from_id, args.to_id, max_depth=args.max_depth)
    if not path:
        print("no path", file=sys.stderr)
        app.emit(None, "")
        return 1
    app.emit([_node_dict(n) for n in path], "  ->  ".join(n.id for n in path))
    return 0


def cmd_neighbors(app: App, args) -> int:
    nodes = list(app.graph.neighborhood(args.id, depth=args.depth).values())
    return _emit_nodes(app, nodes)


def cmd_descendants(app: App, args) -> int:
    return _emit_nodes(app, app.graph.descendants(args.id, args.type, max_depth=args.max_depth))


def cmd_events(app: App, args) -> int:
    evs = app.events.tail(args.n)
    app.emit([e.to_dict() for e in evs],
             "\n".join(f"{e.seq:>5}  {e.ts}  {e.actor:<16} {e.op}  {e.id}" for e in evs)
             or "(no events)")
    return 0


# ---- write handlers (gated + logged via service) ---------------------


def cmd_node_add(app: App, args) -> int:
    node = service.upsert_node(
        app.graph, app.events,
        id=args.id, kind=args.kind, name=args.name,
        labels=args.label, properties=_parse_props(args.prop), actor=args.actor,
    )
    app.emit(_node_dict(node), _fmt_node(node))
    return 0


def cmd_node_del(app: App, args) -> int:
    res = service.delete_node(app.graph, app.events, args.id, actor=args.actor)
    app.emit(res, f"deleted {args.id}: {res['deleted']} ({res['edges_removed']} edges removed)")
    return 0 if res["deleted"] else 1


def cmd_node_set_prop(app: App, args) -> int:
    node = service.set_property(
        app.graph, app.events, args.id, args.key, _parse_prop_value(args.value), actor=args.actor
    )
    if node is None:
        print(f"no node {args.id!r}", file=sys.stderr)
        return 1
    app.emit(_node_dict(node), _fmt_node(node))
    return 0


def cmd_node_add_label(app: App, args) -> int:
    node = service.set_label(app.graph, app.events, args.id, args.label, actor=args.actor)
    if node is None:
        print(f"no node {args.id!r}", file=sys.stderr)
        return 1
    app.emit(_node_dict(node), _fmt_node(node))
    return 0


def cmd_edge_add(app: App, args) -> int:
    edge = service.add_edge(
        app.graph, app.events, args.from_id, args.to_id, args.type,
        properties=_parse_props(args.prop), actor=args.actor,
    )
    app.emit(_edge_dict(edge), f"{edge.from_node} -[{edge.type}]-> {edge.to_node}")
    return 0


def cmd_edge_rm(app: App, args) -> int:
    res = service.remove_edge(app.graph, app.events, args.from_id, args.to_id, args.type, actor=args.actor)
    app.emit(res, f"removed {res['removed']} edge(s): {args.from_id} -[{args.type}]-> {args.to_id}")
    return 0 if res["removed"] else 1


def cmd_revert(app: App, args) -> int:
    comp = service.revert_event(app.events, args.event_id, actor=args.actor)
    app.emit(comp.to_dict(), f"reverted {args.event_id} via {comp.op} event {comp.id}")
    return 0


def cmd_replay(app: App, args) -> int:
    res = service.replay_log(app.graph, app.events, upto_ts=args.upto)
    app.emit(res, f"replayed {res['events_applied']} events (upto {res['upto_ts']}); "
                  f"{app.graph.total_nodes():,} nodes after rebuild")
    return 0


def cmd_import(app: App, args) -> int:
    """Bulk import a {"nodes": [...], "edges": [...]} JSON document.

    Delegates to `service.import_graph` — one gated + logged batch (the same path
    the MCP `kg_import` tool uses), so a bulk load is fast, fully recorded, and
    survives a later `kg replay`.
    """
    with open(args.file, encoding="utf-8") as fh:
        doc = json.load(fh)
    res = service.import_graph(
        app.graph, app.events,
        nodes=doc.get("nodes", []), edges=doc.get("edges", []), actor=args.actor,
    )
    app.emit(res, f"imported {res['nodes_imported']:,} nodes and "
                  f"{res['edges_imported']:,} edges (gated + logged)")
    return 0


def cmd_rdf_export(app: App, args) -> int:
    """Serialize the whole graph to RDF (Turtle/N-Triples, RDF-star by default).

    Export is dependency-free. `--edge-strategy` chooses how edge properties
    cross: rdf-star (quoted triples, default), reification (rdf:Statement), or
    lossy (bare triples — the dropped count is reported, never silent).
    """
    ctx = rdf.IriContext(edge_strategy=args.edge_strategy)
    triples = rdf.export_graph(app.graph, ctx)
    if args.format in ("turtle", "ttl"):
        text = rdf.to_turtle(triples, ctx)
    else:
        text = rdf.to_ntriples(triples)
    dropped = getattr(triples, "dropped_edge_props", 0)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        note = f" ({dropped} edge-property values dropped by lossy)" if dropped else ""
        app.emit(
            {"format": args.format, "triples": len(triples), "out": args.out, "dropped_edge_props": dropped},
            f"wrote {len(triples):,} triples to {args.out}{note}",
        )
    elif app.as_json:
        app.emit({"format": args.format, "triples": len(triples), "dropped_edge_props": dropped, "rdf": text})
    else:
        if dropped:
            print(f"# note: {dropped} edge-property values dropped by lossy strategy", file=sys.stderr)
        print(text, end="")  # raw RDF to stdout, pipeable
    return 0


def cmd_rdf_import(app: App, args) -> int:
    """Load RDF into the graph through the gated + logged path (replayable).

    N-Triples import is dependency-free; Turtle import needs the 'rdf' extra
    (rdflib). `--edge-strategy` must match how the RDF encoded its edges.
    """
    with open(args.file, encoding="utf-8") as fh:
        text = fh.read()
    ctx = rdf.IriContext(edge_strategy=args.edge_strategy)
    res = rdf.import_rdf(app.graph, app.events, text, fmt=args.format, ctx=ctx, actor=args.actor)
    app.emit(res, f"imported {res['nodes_imported']:,} nodes and "
                  f"{res['edges_imported']:,} edges from {args.file} (gated + logged)")
    return 0


def cmd_serve(app: App, args) -> int:
    try:
        from kgrdbms import mcp_server
    except ImportError:
        print("the MCP server needs the 'mcp' extra: pip install knowledge-graph-rdbms[mcp]",
              file=sys.stderr)
        return 1
    app.close()  # the server opens its own graph
    mcp_server.serve(transport=args.transport)
    return 0


# ---- parser ----------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kg", description="Command-line interface to a kgrdbms graph.")
    p.add_argument("--version", action="version", version=f"kgrdbms {__version__}")
    p.add_argument("--ontology", help="named ontology to target via the resolver (default: the default ontology)")
    p.add_argument("--db", help="raw escape hatch: open this exact db file directly, bypassing the registry")
    p.add_argument("--json", dest="as_json", action="store_true", help="emit JSON instead of text")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("stats", help="node/edge counts, db path, active ontology")
    sp.set_defaults(func=cmd_stats)

    # ---- ontology registry (the db-of-dbs) ----
    ont = sub.add_parser("ontology", help="manage the ontology registry").add_subparsers(dest="action", required=True)
    a = ont.add_parser("list", help="list registered ontologies")
    a.set_defaults(func=cmd_ontology_list)
    a = ont.add_parser("create", help="register a new ontology (name, backend, opinion)")
    a.add_argument("name")
    a.add_argument("--backend", default="sqlite", help="engine: sqlite | postgres (live) | neo4j (stub)")
    a.add_argument("--location", help="backend location: a DSN for postgres (postgresql://…); "
                                      "omit for a managed sqlite file")
    a.add_argument("--description", default="")
    a.add_argument("--stance", default="literal", help="extraction opinion: literal | inferential | ...")
    a.set_defaults(func=cmd_ontology_create)

    # ---- node group ----
    node = sub.add_parser("node", help="node operations").add_subparsers(dest="action", required=True)

    a = node.add_parser("add", help="create or update a node")
    a.add_argument("id")
    a.add_argument("--kind", required=True)
    a.add_argument("--name")
    a.add_argument("--label", action="append", help="repeatable")
    a.add_argument("--prop", action="append", metavar="KEY=VALUE", help="repeatable; value parsed as JSON then string")
    a.add_argument("--actor", default="cli")
    a.set_defaults(func=cmd_node_add)

    a = node.add_parser("get", help="fetch a node by id")
    a.add_argument("id")
    a.set_defaults(func=cmd_node_get)

    a = node.add_parser("del", help="delete a node (cascades its edges)")
    a.add_argument("id")
    a.add_argument("--actor", default="cli")
    a.set_defaults(func=cmd_node_del)

    a = node.add_parser("set-prop", help="set one property on a node")
    a.add_argument("id")
    a.add_argument("key")
    a.add_argument("value")
    a.add_argument("--actor", default="cli")
    a.set_defaults(func=cmd_node_set_prop)

    a = node.add_parser("add-label", help="add a label to a node")
    a.add_argument("id")
    a.add_argument("label")
    a.add_argument("--actor", default="cli")
    a.set_defaults(func=cmd_node_add_label)

    # ---- edge group ----
    edge = sub.add_parser("edge", help="edge operations").add_subparsers(dest="action", required=True)

    a = edge.add_parser("add", help="add an edge (from, to, type)")
    a.add_argument("from_id", metavar="FROM")
    a.add_argument("to_id", metavar="TO")
    a.add_argument("type")
    a.add_argument("--prop", action="append", metavar="KEY=VALUE")
    a.add_argument("--actor", default="cli")
    a.set_defaults(func=cmd_edge_add)

    a = edge.add_parser("rm", help="remove an edge (from, to, type)")
    a.add_argument("from_id", metavar="FROM")
    a.add_argument("to_id", metavar="TO")
    a.add_argument("type")
    a.add_argument("--actor", default="cli")
    a.set_defaults(func=cmd_edge_rm)

    # ---- queries / traversal ----
    a = sub.add_parser("nodes-by-kind", help="list all nodes of a kind")
    a.add_argument("kind")
    a.set_defaults(func=cmd_nodes_by_kind)

    a = sub.add_parser("nodes-by-label", help="list all nodes carrying a label")
    a.add_argument("label")
    a.set_defaults(func=cmd_nodes_by_label)

    a = sub.add_parser("out", help="outbound edges of a node")
    a.add_argument("id")
    a.add_argument("--type", help="filter by edge type")
    a.set_defaults(func=cmd_out)

    a = sub.add_parser("in", help="inbound edges of a node")
    a.add_argument("id")
    a.add_argument("--type", help="filter by edge type")
    a.set_defaults(func=cmd_in)

    a = sub.add_parser("path", help="shortest undirected path between two nodes")
    a.add_argument("from_id", metavar="FROM")
    a.add_argument("to_id", metavar="TO")
    a.add_argument("--max-depth", type=int, default=8, dest="max_depth")
    a.set_defaults(func=cmd_path)

    a = sub.add_parser("neighbors", help="nodes within N undirected hops")
    a.add_argument("id")
    a.add_argument("--depth", type=int, default=1)
    a.set_defaults(func=cmd_neighbors)

    a = sub.add_parser("descendants", help="nodes reachable along one edge type")
    a.add_argument("id")
    a.add_argument("type")
    a.add_argument("--max-depth", type=int, default=6, dest="max_depth")
    a.set_defaults(func=cmd_descendants)

    # ---- event log ----
    a = sub.add_parser("events", help="tail the event log")
    a.add_argument("-n", type=int, default=20, dest="n")
    a.set_defaults(func=cmd_events)

    a = sub.add_parser("revert", help="reverse an event by id (compensating event)")
    a.add_argument("event_id", metavar="EVENT_ID")
    a.add_argument("--actor", default="cli")
    a.set_defaults(func=cmd_revert)

    a = sub.add_parser("replay", help="rebuild the projection from the log")
    a.add_argument("--upto", help="ISO timestamp to project to (time travel)")
    a.set_defaults(func=cmd_replay)

    # ---- bulk + server ----
    a = sub.add_parser("import", help="bulk import a {nodes, edges} JSON file (gated + logged)")
    a.add_argument("file")
    a.add_argument("--actor", default="cli-import")
    a.set_defaults(func=cmd_import)

    # ---- RDF boundary ----
    rdfp = sub.add_parser("rdf", help="export/import RDF (Turtle / N-Triples, RDF-star)")
    rdfsub = rdfp.add_subparsers(dest="action", required=True)

    a = rdfsub.add_parser("export", help="serialize the graph to RDF (dependency-free)")
    a.add_argument("--format", default="turtle", choices=["turtle", "ttl", "ntriples", "nt"],
                   help="turtle (default, human-readable) or ntriples (lossless, dep-free)")
    a.add_argument("--edge-strategy", dest="edge_strategy", default="rdf-star",
                   choices=["rdf-star", "reification", "lossy"],
                   help="how edge properties cross the boundary (default: rdf-star)")
    a.add_argument("--out", help="write to this file (default: stdout)")
    a.set_defaults(func=cmd_rdf_export)

    a = rdfsub.add_parser("import", help="load RDF into the graph (gated + logged)")
    a.add_argument("file")
    a.add_argument("--format", default="ntriples", choices=["ntriples", "nt", "turtle", "ttl"],
                   help="ntriples (default, dep-free) or turtle (needs the 'rdf' extra)")
    a.add_argument("--edge-strategy", dest="edge_strategy", default="rdf-star",
                   choices=["rdf-star", "reification", "lossy"],
                   help="strategy the RDF was written with, so edges decode correctly")
    a.add_argument("--actor", default="rdf-import")
    a.set_defaults(func=cmd_rdf_import)

    a = sub.add_parser("serve", help="run the MCP server (needs the 'mcp' extra)")
    a.add_argument("--transport", default="stdio", choices=["stdio", "sse", "streamable-http"])
    a.set_defaults(func=cmd_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    app = App(db=args.db, ontology=getattr(args, "ontology", None), as_json=args.as_json)
    try:
        return args.func(app, args)
    except InvariantViolation as e:
        print(f"refused (invariant): {e}", file=sys.stderr)
        return 3
    except PermissionError as e:
        print(f"refused (policy): {e}", file=sys.stderr)
        return 2
    except NotImplementedError as e:
        print(f"unavailable: {e}", file=sys.stderr)
        return 1
    except (KeyError, ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        app.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
