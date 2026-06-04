"""kgrdbms.rdf — the RDF boundary (export now; import next).

Design stance (see CLAUDE.md "No RDF stack"): the store stays a label
property graph. RDF is a *boundary* format, not a storage model. No OWL, no
SPARQL, no triplestore. We adopted exactly one RDF idea because it is the only
one expensive to retrofit — stable identity (CURIEs). This module is where a
CURIE (`person:ada-lovelace`) finally expands into a real IRI
(`<https://kg.local/person/ada-lovelace>`), the day you actually publish.

Two promises this module keeps:

  * **Export is dependency-free.** Writing N-Triples / Turtle is just careful
    formatting; the zero-dependency core survives export intact.
  * **Enumeration uses only the existing backend surface.** There is no
    `all_nodes()` on `GraphBackend`. We don't add one: kinds come from
    `count_nodes_by_kind()`, nodes from `nodes_by_kind(kind)`, edges from
    `out(node)`. So this works over SQLite *and* Postgres with zero protocol
    change.

Rich *import* (parsing arbitrary Turtle / RDF-XML) is the part that genuinely
needs a parser; that will lazily import `rdflib` behind the optional [rdf]
extra (mirroring how postgres.py lazily imports psycopg). N-Triples import,
being strictly line-oriented, can stay dependency-free.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterator

from kgrdbms.backends.base import GraphBackend
from kgrdbms.graph import Edge, Node


# ---- RDF term model --------------------------------------------------
#
# We only need two kinds of RDF term to serialize an LPG: IRIs (subjects,
# predicates, and object references) and typed literals (property values).


@dataclass(frozen=True)
class Iri:
    """An absolute IRI. Serialized as <...> in N-Triples/Turtle."""
    value: str


@dataclass(frozen=True)
class Literal:
    """A typed RDF literal: a lexical form plus an optional xsd datatype IRI."""
    lexical: str
    datatype: str | None = None  # e.g. "http://www.w3.org/2001/XMLSchema#integer"


@dataclass(frozen=True)
class Quoted:
    """An RDF-star quoted triple — a triple used as a term (subject or object).

    Serialized as `<< s p o >>`. This is the one construct that lets an edge's
    properties attach to the *edge itself* (`<< :ada :knows :grace >> :since 2020`)
    instead of being lost or reified into four extra triples. It is frozen so it
    can nest inside another Triple as a hashable term.
    """
    triple: "Triple"


# A triple's subject may be an IRI or (RDF-star) a quoted triple; the object may
# additionally be a Literal. Predicates are always IRIs.
Triple = tuple["Iri | Quoted", Iri, "Iri | Literal | Quoted"]


# Well-known namespaces we lean on.
RDF = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
XSD = "http://www.w3.org/2001/XMLSchema#"
# The kg: vocabulary — our small reserved namespace for LPG constructs that
# have no standard RDF predicate (a node's `kind`, its `labels`).
KG = "https://kg.local/vocab#"


# ---- IRI context: the CURIE -> IRI expansion table -------------------


@dataclass
class IriContext:
    """The lookup table CLAUDE.md promised you'd only need "the day you publish".

    A CURIE prefix (`person`) binds to a base IRI; expanding `person:ada` means
    concatenating the base with the slugged reference. Property keys and edge
    types get their own bases so the output is navigable, not opaque.
    """

    # prefix -> base IRI. The fallback base handles any prefix not listed.
    prefix_bases: dict[str, str] = field(default_factory=dict)
    default_base: str = "https://kg.local/"
    prop_base: str = "https://kg.local/prop/"   # node/edge property predicates
    edge_base: str = "https://kg.local/rel/"    # edge-type predicates
    # How edge.properties cross the boundary: "rdf-star" (quoted triples),
    # "reification" (rdf:Statement nodes), or "lossy" (bare triple only).
    edge_strategy: str = "rdf-star"

    def expand_node(self, node_id: str) -> Iri:
        """`person:ada-lovelace` -> <https://kg.local/person/ada-lovelace>."""
        if ":" in node_id:
            prefix, ref = node_id.split(":", 1)
            base = self.prefix_bases.get(prefix, f"{self.default_base}{prefix}/")
        else:
            prefix, ref, base = "", node_id, self.default_base
        return Iri(f"{base}{ref}")

    def prop_predicate(self, key: str) -> Iri:
        return Iri(f"{self.prop_base}{key}")

    def edge_predicate(self, edge_type: str) -> Iri:
        return Iri(f"{self.edge_base}{edge_type}")


# ---- value -> literal typing -----------------------------------------


def literal_for(value: Any) -> Literal:
    """Map a JSON-shaped property value to a typed RDF literal.

    Mirrors how properties round-trip as JSON in the store: ints, bools and
    floats get their xsd datatypes; strings stay plain; lists/objects are
    emitted as a JSON string literal (lossless, if not idiomatic RDF).
    """
    # bool must precede int — bool is an int subclass in Python.
    if isinstance(value, bool):
        return Literal("true" if value else "false", f"{XSD}boolean")
    if isinstance(value, int):
        return Literal(str(value), f"{XSD}integer")
    if isinstance(value, float):
        return Literal(repr(value), f"{XSD}double")
    if isinstance(value, str):
        return Literal(value)  # plain literal (implicitly xsd:string)
    # list / dict / None -> JSON literal, so nothing is silently dropped.
    return Literal(json.dumps(value), f"{KG}json")


# ---- graph enumeration (over the existing backend surface) -----------


def iter_nodes(backend: GraphBackend) -> Iterator[Node]:
    """Every node, walked via kinds -> nodes_by_kind. No protocol change."""
    for kind in backend.count_nodes_by_kind():
        yield from backend.nodes_by_kind(kind)


def iter_edges(backend: GraphBackend) -> Iterator[tuple[Node, Edge, Node]]:
    """Every edge as (from_node, edge, to_node), walked via out()."""
    for node in iter_nodes(backend):
        for edge, target in backend.out(node.id):
            yield node, edge, target


# ---- LPG -> triples --------------------------------------------------


def node_to_triples(node: Node, ctx: IriContext) -> list[Triple]:
    """A node becomes: an rdf:type for its kind, one kg:label per label, and
    one property triple per key. (Edges are handled separately.)"""
    s = ctx.expand_node(node.id)
    triples: list[Triple] = [
        # kind -> rdf:type, pointing at a class IRI under the kg vocab.
        (s, Iri(f"{RDF}type"), Iri(f"{KG}{node.kind}")),
    ]
    if node.name:
        triples.append((s, Iri(f"{KG}name"), Literal(node.name)))
    for label in sorted(node.labels):
        triples.append((s, Iri(f"{KG}label"), Literal(label)))
    for key, value in node.properties.items():
        triples.append((s, ctx.prop_predicate(key), literal_for(value)))
    return triples


def edge_to_triples(node_from: Node, edge: Edge, node_to: Node, ctx: IriContext) -> list[Triple]:
    """Map one LPG edge — INCLUDING its properties — to RDF triples.

    The base triple `:ada :influences :grace` is always asserted. The edge's
    *properties* are the hard part (a plain triple has nowhere to hang them);
    `ctx.edge_strategy` chooses how they cross:

      * "rdf-star"    — `<< :ada :influences :grace >> :since 2020` (default).
                        Clean, modern (RDF 1.2), and lossless. Requires a
                        star-aware consumer.
      * "reification" — an rdf:Statement node carrying subject/predicate/object
                        plus each property. Verbose (4+ extra triples) but works
                        in every RDF tool since 2004.
      * "lossy"       — bare triple only; edge.properties dropped (and counted,
                        see export_graph — never silently).
    """
    s = ctx.expand_node(node_from.id)
    p = ctx.edge_predicate(edge.type)
    o = ctx.expand_node(node_to.id)
    base: Triple = (s, p, o)
    triples: list[Triple] = [base]

    if not edge.properties:
        return triples

    if ctx.edge_strategy == "rdf-star":
        # Annotate the *quoted* triple — the edge itself becomes the subject.
        qt = Quoted(base)
        for key, value in edge.properties.items():
            triples.append((qt, ctx.prop_predicate(key), literal_for(value)))
    elif ctx.edge_strategy == "reification":
        stmt = Iri(f"{ctx.default_base}stmt/{edge.id}")
        triples += [
            (stmt, Iri(f"{RDF}type"), Iri(f"{RDF}Statement")),
            (stmt, Iri(f"{RDF}subject"), s),
            (stmt, Iri(f"{RDF}predicate"), p),
            (stmt, Iri(f"{RDF}object"), o),
        ]
        for key, value in edge.properties.items():
            triples.append((stmt, ctx.prop_predicate(key), literal_for(value)))
    elif ctx.edge_strategy == "lossy":
        pass  # bare triple only; caller is responsible for surfacing the drop
    else:
        raise ValueError(f"unknown edge_strategy: {ctx.edge_strategy!r}")

    return triples


# ---- serializers (dependency-free) -----------------------------------


def _escape_literal(lexical: str) -> str:
    return (
        lexical.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _term_ntriples(term: Iri | Literal | Quoted) -> str:
    if isinstance(term, Quoted):
        s, p, o = term.triple
        return f"<< {_term_ntriples(s)} {_term_ntriples(p)} {_term_ntriples(o)} >>"
    if isinstance(term, Iri):
        return f"<{term.value}>"
    esc = _escape_literal(term.lexical)
    if term.datatype:
        return f'"{esc}"^^<{term.datatype}>'
    return f'"{esc}"'


def to_ntriples(triples: list[Triple]) -> str:
    """Serialize as N-Triples-star (one statement per line; `<< >>` for edges)."""
    lines = []
    for s, p, o in triples:
        lines.append(f"{_term_ntriples(s)} {_term_ntriples(p)} {_term_ntriples(o)} .")
    return "\n".join(lines) + ("\n" if lines else "")


# -- Turtle: prefix shortening so the output round-trips back to CURIEs --

_PN_LOCAL = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*$")


def _split_ns(iri: str) -> tuple[str, str]:
    """Split an IRI into (namespace, local) at the last '#' or '/'."""
    for i in range(len(iri) - 1, -1, -1):
        if iri[i] in "#/":
            return iri[: i + 1], iri[i + 1 :]
    return "", iri


def _derive_label(ns: str) -> str:
    seg = re.split(r"[/#]", ns.rstrip("#/"))[-1]
    seg = re.sub(r"[^A-Za-z0-9_]", "", seg) or "ns"
    return ("ns" + seg) if seg[0].isdigit() else seg


def _iter_iris(triples: list[Triple]) -> Iterator[str]:
    """Every IRI string appearing anywhere (terms, nested quoted triples, datatypes)."""
    def walk(term: Iri | Literal | Quoted) -> Iterator[str]:
        if isinstance(term, Quoted):
            for t in term.triple:
                yield from walk(t)
        elif isinstance(term, Iri):
            yield term.value
        elif isinstance(term, Literal) and term.datatype:
            yield term.datatype
    for tr in triples:
        for term in tr:
            yield from walk(term)


def _prefix_table(triples: list[Triple], ctx: IriContext) -> dict[str, str]:
    """Build namespace -> short-label bindings (fixed vocab + data-derived)."""
    table = {RDF: "rdf", XSD: "xsd", KG: "kg", ctx.prop_base: "prop", ctx.edge_base: "rel"}
    used = set(table.values())
    for iri in _iter_iris(triples):
        ns, local = _split_ns(iri)
        if not ns or ns in table or not _PN_LOCAL.match(local):
            continue
        label = _derive_label(ns)
        while label in used:
            label += "_"
        table[ns] = label
        used.add(label)
    return table


def _shorten(iri: str, table: dict[str, str]) -> str:
    ns, local = _split_ns(iri)
    if ns in table and _PN_LOCAL.match(local):
        return f"{table[ns]}:{local}"
    return f"<{iri}>"


def _term_turtle(term: Iri | Literal | Quoted, table: dict[str, str]) -> str:
    if isinstance(term, Quoted):
        s, p, o = term.triple
        return f"<< {_term_turtle(s, table)} {_term_turtle(p, table)} {_term_turtle(o, table)} >>"
    if isinstance(term, Iri):
        return _shorten(term.value, table)
    esc = _escape_literal(term.lexical)
    if term.datatype:
        return f'"{esc}"^^{_shorten(term.datatype, table)}'
    return f'"{esc}"'


def to_turtle(triples: list[Triple], ctx: IriContext | None = None) -> str:
    """Serialize as Turtle-star: @prefix header + subject-grouped statements."""
    ctx = ctx or IriContext()
    table = _prefix_table(triples, ctx)
    header = "\n".join(
        f"@prefix {label}: <{ns}> ." for ns, label in sorted(table.items(), key=lambda kv: kv[1])
    )

    # Group by subject, preserving first-seen order.
    groups: dict[str, tuple[Iri | Quoted, list[tuple[Iri, Iri | Literal | Quoted]]]] = {}
    order: list[str] = []
    for s, p, o in triples:
        key = _term_turtle(s, table)
        if key not in groups:
            groups[key] = (s, [])
            order.append(key)
        groups[key][1].append((p, o))

    blocks = []
    for key in order:
        subj, pos = groups[key]
        preds = []
        for p, o in pos:
            pstr = "a" if (isinstance(p, Iri) and p.value == f"{RDF}type") else _term_turtle(p, table)
            preds.append(f"{pstr} {_term_turtle(o, table)}")
        blocks.append(_term_turtle(subj, table) + " " + " ;\n    ".join(preds) + " .")

    body = "\n\n".join(blocks)
    return f"{header}\n\n{body}\n" if blocks else header + "\n"


class TripleList(list):
    """A list of triples that also remembers how many edge properties the
    chosen strategy dropped — so 'lossy' export is never *silently* lossy."""
    dropped_edge_props: int = 0


def export_graph(backend: GraphBackend, ctx: IriContext | None = None) -> TripleList:
    """Walk the whole graph and return every triple (nodes then edges).

    When ctx.edge_strategy == "lossy", edge properties are dropped — but never
    silently: the count of dropped property values is recorded on the returned
    list's `.dropped_edge_props` attribute for the caller to surface.
    """
    ctx = ctx or IriContext()
    triples = TripleList()
    dropped = 0
    for node in iter_nodes(backend):
        triples.extend(node_to_triples(node, ctx))
    for nf, edge, nt in iter_edges(backend):
        if ctx.edge_strategy == "lossy":
            dropped += len(edge.properties)
        triples.extend(edge_to_triples(nf, edge, nt, ctx))
    triples.dropped_edge_props = dropped
    return triples


def export(backend: GraphBackend, fmt: str = "turtle", ctx: IriContext | None = None) -> str:
    """Export the whole graph as a string in `fmt` ('turtle'/'ttl' or 'ntriples'/'nt')."""
    ctx = ctx or IriContext()
    triples = export_graph(backend, ctx)
    if fmt in ("turtle", "ttl"):
        return to_turtle(triples, ctx)
    if fmt in ("ntriples", "nt"):
        return to_ntriples(triples)
    raise ValueError(f"unknown rdf format: {fmt!r} (use 'turtle' or 'ntriples')")


# ======================================================================
# IMPORT — RDF text back into the LPG, through the logged service path.
# ======================================================================
#
# N-Triples(-star) parsing is dependency-free (the format is line-oriented and
# our own export is the round-trip target). Turtle / foreign RDF parsing lazily
# imports rdflib (the [rdf] extra) — that's the only thing that genuinely needs
# a full parser, mirroring how postgres.py lazily imports psycopg.


def _unescape(s: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            out.append({"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}.get(s[i + 1], s[i + 1]))
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def _skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in " \t":
        i += 1
    return i


def _read_term(s: str, i: int) -> tuple[Iri | Literal | Quoted, int]:
    """Read one N-Triples(-star) term starting at i; return (term, next_index)."""
    i = _skip_ws(s, i)
    if s.startswith("<<", i):  # RDF-star quoted triple
        sub, i = _read_term(s, i + 2)
        pred, i = _read_term(s, i)
        obj, i = _read_term(s, i)
        i = _skip_ws(s, i)
        if not s.startswith(">>", i):
            raise ValueError(f"expected '>>' at {i} in {s!r}")
        return Quoted((sub, pred, obj)), i + 2  # type: ignore[arg-type]
    if s[i] == "<":  # IRI
        j = s.index(">", i)
        return Iri(s[i + 1 : j]), j + 1
    if s[i] == '"':  # literal
        j = i + 1
        buf: list[str] = []
        while j < len(s):
            if s[j] == "\\":
                buf.append(s[j : j + 2])
                j += 2
                continue
            if s[j] == '"':
                break
            buf.append(s[j])
            j += 1
        lexical = _unescape("".join(buf))
        j += 1  # past the closing quote
        datatype = None
        if s.startswith("^^", j):
            k = s.index("<", j)
            m = s.index(">", k)
            datatype = s[k + 1 : m]
            j = m + 1
        return Literal(lexical, datatype), j
    raise ValueError(f"cannot parse term at {i}: {s[i : i + 24]!r}")


def parse_ntriples(text: str) -> list[Triple]:
    """Parse N-Triples-star text into our Triple model (dependency-free)."""
    triples: list[Triple] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        s, i = _read_term(line, 0)
        p, i = _read_term(line, i)
        o, i = _read_term(line, i)
        triples.append((s, p, o))  # type: ignore[arg-type]
    return triples


def literal_to_value(lit: Literal) -> Any:
    """Invert literal_for(): an RDF literal back to a JSON-shaped Python value."""
    dt = lit.datatype
    if dt is None:
        return lit.lexical
    if dt == f"{XSD}integer":
        return int(lit.lexical)
    if dt == f"{XSD}boolean":
        return lit.lexical == "true"
    if dt == f"{XSD}double":
        return float(lit.lexical)
    if dt == f"{KG}json":
        return json.loads(lit.lexical)
    return lit.lexical


def contract_iri(iri: str, ctx: IriContext) -> str:
    """Invert IriContext.expand_node(): an IRI back to its CURIE node id."""
    # Explicit prefix bindings win (longest base first to avoid prefix overlap).
    for prefix, base in sorted(ctx.prefix_bases.items(), key=lambda kv: -len(kv[1])):
        if iri.startswith(base):
            return f"{prefix}:{iri[len(base):]}"
    if iri.startswith(ctx.default_base):
        rest = iri[len(ctx.default_base):]
        if "/" in rest:
            prefix, ref = rest.split("/", 1)
            return f"{prefix}:{ref}"
        return rest
    return iri  # foreign IRI — keep verbatim


def _local_after(iri: str, base: str) -> str | None:
    return iri[len(base):] if iri.startswith(base) else None


def triples_to_graph(triples: list[Triple], ctx: IriContext | None = None) -> tuple[list[dict], list[dict]]:
    """Invert export: triples -> (node specs, edge specs) for service.import_graph.

    Understands all three edge strategies:
      * rdf-star    — a Quoted-triple subject carries the edge's properties
      * reification — an rdf:Statement node reassembles into an edge + properties
      * lossy       — bare triples become property-less edges
    """
    ctx = ctx or IriContext()
    nodes: dict[str, dict] = {}
    edges: dict[tuple[str, str, str], dict] = {}
    reif: dict[str, dict] = {}

    def node(nid: str) -> dict:
        return nodes.setdefault(nid, {"id": nid, "kind": None, "name": None, "labels": [], "properties": {}})

    def edge(f: str, t: str, ty: str) -> dict:
        return edges.setdefault((f, ty, t), {"from": f, "to": t, "type": ty, "properties": {}})

    # Pre-scan: which subjects are reification statements?
    stmt_ids = {
        s.value
        for s, p, o in triples
        if isinstance(s, Iri) and isinstance(p, Iri) and p.value == f"{RDF}type"
        and isinstance(o, Iri) and o.value == f"{RDF}Statement"
    }

    for s, p, o in triples:
        pv = p.value

        if isinstance(s, Quoted):  # rdf-star edge annotation
            qs, qp, qo = s.triple
            if isinstance(qs, Iri) and isinstance(qo, Iri):
                f, t = contract_iri(qs.value, ctx), contract_iri(qo.value, ctx)
                ty = _local_after(qp.value, ctx.edge_base) or qp.value
                e = edge(f, t, ty)
                key = _local_after(pv, ctx.prop_base)
                if key is not None and isinstance(o, Literal):
                    e["properties"][key] = literal_to_value(o)
            continue

        sid = s.value

        if sid in stmt_ids:  # reification statement parts
            r = reif.setdefault(sid, {"properties": {}})
            if pv == f"{RDF}subject":
                r["s"] = o
            elif pv == f"{RDF}predicate":
                r["p"] = o
            elif pv == f"{RDF}object":
                r["o"] = o
            elif pv != f"{RDF}type":
                key = _local_after(pv, ctx.prop_base)
                if key is not None and isinstance(o, Literal):
                    r["properties"][key] = literal_to_value(o)
            continue

        # otherwise: an ordinary node subject
        nid = contract_iri(sid, ctx)
        n = node(nid)
        if pv == f"{RDF}type" and isinstance(o, Iri):
            kind = _local_after(o.value, KG)
            if kind is not None:
                n["kind"] = kind
        elif pv == f"{KG}name" and isinstance(o, Literal):
            n["name"] = o.lexical
        elif pv == f"{KG}label" and isinstance(o, Literal):
            n["labels"].append(o.lexical)
        elif pv.startswith(ctx.edge_base) and isinstance(o, Iri):
            edge(nid, contract_iri(o.value, ctx), _local_after(pv, ctx.edge_base))
        elif pv.startswith(ctx.prop_base) and isinstance(o, Literal):
            n["properties"][_local_after(pv, ctx.prop_base)] = literal_to_value(o)
        # foreign predicates are ignored (we only re-import what we emit)

    # Materialize reified edges.
    for r in reif.values():
        if {"s", "p", "o"} <= r.keys() and isinstance(r["s"], Iri) and isinstance(r["o"], Iri):
            ty = _local_after(r["p"].value, ctx.edge_base) or r["p"].value
            e = edge(contract_iri(r["s"].value, ctx), contract_iri(r["o"].value, ctx), ty)
            e["properties"].update(r["properties"])

    # Finalize: a node referenced but never typed defaults to kind 'Thing'.
    node_specs = []
    for spec in nodes.values():
        spec["kind"] = spec["kind"] or "Thing"
        spec["name"] = spec["name"] or spec["id"]
        node_specs.append(spec)
    # Endpoints that only appeared inside edges still need to exist.
    for (f, ty, t) in edges:
        for endpoint in (f, t):
            if endpoint not in nodes:
                node_specs.append({"id": endpoint, "kind": "Thing", "name": endpoint, "labels": [], "properties": {}})
                nodes[endpoint] = {}  # mark as seen

    return node_specs, list(edges.values())


def _rdflib_to_triples(text: str, fmt: str) -> list[Triple]:
    """Parse Turtle / foreign RDF via rdflib (lazy import; needs the [rdf] extra)."""
    try:
        import rdflib
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised without the extra
        raise NotImplementedError(
            "turtle/foreign-RDF import needs the 'rdf' extra: pip install 'knowledge-graph-rdbms[rdf]'"
        ) from exc
    store = rdflib.Graph()
    store.parse(data=text, format="turtle" if fmt in ("turtle", "ttl") else fmt)
    out: list[Triple] = []
    for s, p, o in store:
        subj = Iri(str(s))
        pred = Iri(str(p))
        if isinstance(o, rdflib.Literal):
            obj: Iri | Literal = Literal(str(o), str(o.datatype) if o.datatype else None)
        else:
            obj = Iri(str(o))
        out.append((subj, pred, obj))
    return out


def parse_rdf(text: str, fmt: str = "ntriples", ctx: IriContext | None = None) -> tuple[list[dict], list[dict]]:
    """Parse RDF text to (node specs, edge specs). nt is dep-free; turtle uses rdflib."""
    if fmt in ("ntriples", "nt"):
        triples = parse_ntriples(text)
    elif fmt in ("turtle", "ttl"):
        triples = _rdflib_to_triples(text, fmt)
    else:
        raise ValueError(f"unknown rdf format: {fmt!r} (use 'ntriples' or 'turtle')")
    return triples_to_graph(triples, ctx)


def import_rdf(backend, events, text: str, *, fmt: str = "ntriples",
               ctx: IriContext | None = None, actor: str = "rdf-import") -> dict:
    """Parse RDF text and apply it through the gated, logged, replayable import path."""
    from kgrdbms import service  # lazy: avoid an import cycle (service -> graph/events)

    nodes, edges = parse_rdf(text, fmt, ctx)
    return service.import_graph(backend, events, nodes=nodes, edges=edges, actor=actor)
