"""Tests for the RDF boundary (kgrdbms.rdf): export, parse, and round-trip.

The core promise under test: an LPG can be serialized to RDF and read back
*losslessly* — including edge properties, the construct RDF makes awkward.
Export is dependency-free; the rdflib-backed Turtle import is gated behind an
importorskip so the suite stays green without the [rdf] extra.
"""

from __future__ import annotations

import os
import tempfile

import pytest

from kgrdbms.events import EventLog
from kgrdbms.graph import Graph
from kgrdbms import rdf


# ---- fixtures --------------------------------------------------------


def _fresh_graph() -> Graph:
    path = os.path.join(tempfile.mkdtemp(), "g.db")
    return Graph(path=path)


@pytest.fixture
def populated() -> Graph:
    g = _fresh_graph()
    g.add_node(
        "person:ada", kind="Person", name="Ada Lovelace",
        labels=["Mathematician", "Pioneer"],
        properties={"born": 1815, "notable": True, "score": 9.5, "aka": ["Countess"]},
    )
    g.add_node("person:grace", kind="Person", name="Grace Hopper", properties={"born": 1906})
    g.add_edge("person:ada", "person:grace", "influences", properties={"since": 2020, "weight": 0.8})
    g.add_edge("person:grace", "person:ada", "admires")  # bare edge, no properties
    return g


def _snapshot(g: Graph) -> tuple[dict, dict]:
    """Structural fingerprint: {id -> (kind, name, labels, props)}, {triple -> props}."""
    nodes = {
        n.id: (n.kind, n.name, tuple(sorted(n.labels)), tuple(sorted(n.properties.items())))
        for kind in g.count_nodes_by_kind()
        for n in g.nodes_by_kind(kind)
    }
    edges = {}
    for nid in nodes:
        for e, _t in g.out(nid):
            edges[(e.from_node, e.type, e.to_node)] = tuple(sorted(e.properties.items()))
    return nodes, edges


def _reimport(text: str, fmt: str, ctx: rdf.IriContext | None = None) -> Graph:
    dst = _fresh_graph()
    rdf.import_rdf(dst, EventLog(dst), text, fmt=fmt, ctx=ctx)
    return dst


# ---- export shape ----------------------------------------------------


def test_export_emits_kind_as_rdf_type(populated):
    triples = rdf.export_graph(populated)
    assert (
        rdf.Iri("https://kg.local/person/ada"),
        rdf.Iri(f"{rdf.RDF}type"),
        rdf.Iri(f"{rdf.KG}Person"),
    ) in triples


def test_literal_typing_round_trips_through_xsd():
    assert rdf.literal_for(1815) == rdf.Literal("1815", f"{rdf.XSD}integer")
    assert rdf.literal_for(True) == rdf.Literal("true", f"{rdf.XSD}boolean")
    assert rdf.literal_for(9.5).datatype == f"{rdf.XSD}double"
    assert rdf.literal_for(["a", "b"]).datatype == f"{rdf.KG}json"
    # bool must not be mistaken for int (bool is an int subclass in Python)
    assert rdf.literal_for(True).datatype.endswith("boolean")


def test_turtle_shortens_iris_back_to_curies(populated):
    ttl = rdf.export(populated, "turtle")
    # The node id round-trips: prefix binding makes the IRI collapse to the CURIE.
    assert "@prefix person: <https://kg.local/person/> ." in ttl
    assert "person:ada a kg:Person ;" in ttl


# ---- edge strategies -------------------------------------------------


def test_rdf_star_annotates_the_quoted_triple(populated):
    nt = rdf.export(populated, "ntriples")  # rdf-star is the default
    assert "<< <https://kg.local/person/ada> <https://kg.local/rel/influences> " in nt
    assert "<https://kg.local/prop/since>" in nt


def test_reification_emits_statement_node(populated):
    ctx = rdf.IriContext(edge_strategy="reification")
    triples = rdf.export_graph(populated, ctx)
    assert any(o == rdf.Iri(f"{rdf.RDF}Statement") for _s, _p, o in triples)


def test_lossy_drops_edge_props_but_keeps_the_triple(populated):
    ctx = rdf.IriContext(edge_strategy="lossy")
    nt = rdf.export(populated, "ntriples", ctx)
    assert "rel/influences" in nt          # the bare edge survives
    assert "prop/since" not in nt          # its properties do not


def test_unknown_strategy_raises(populated):
    ctx = rdf.IriContext(edge_strategy="nonsense")
    with pytest.raises(ValueError):
        rdf.export_graph(populated, ctx)


# ---- parser ----------------------------------------------------------


def test_parse_ntriples_reads_quoted_triples():
    text = (
        "<< <http://x/a> <http://x/b> <http://x/c> >> "
        '<http://x/since> "2020"^^<http://www.w3.org/2001/XMLSchema#integer> .'
    )
    triples = rdf.parse_ntriples(text)
    assert len(triples) == 1
    subj = triples[0][0]
    assert isinstance(subj, rdf.Quoted)
    assert subj.triple[0] == rdf.Iri("http://x/a")


def test_contract_iri_inverts_expansion():
    ctx = rdf.IriContext()
    assert ctx.expand_node("person:ada").value == "https://kg.local/person/ada"
    assert rdf.contract_iri("https://kg.local/person/ada", ctx) == "person:ada"


# ---- round-trips (the real proof) ------------------------------------


def test_round_trip_rdf_star_is_lossless(populated):
    nt = rdf.export(populated, "ntriples")  # default: rdf-star
    dst = _reimport(nt, "ntriples")
    assert _snapshot(dst) == _snapshot(populated)


def test_round_trip_reification_is_lossless(populated):
    ctx = rdf.IriContext(edge_strategy="reification")
    nt = rdf.export(populated, "ntriples", ctx)
    dst = _reimport(nt, "ntriples", ctx)
    assert _snapshot(dst) == _snapshot(populated)


def test_round_trip_lossy_drops_edge_props_only(populated):
    ctx = rdf.IriContext(edge_strategy="lossy")
    nt = rdf.export(populated, "ntriples", ctx)
    dst = _reimport(nt, "ntriples", ctx)
    src_nodes, src_edges = _snapshot(populated)
    dst_nodes, dst_edges = _snapshot(dst)
    assert dst_nodes == src_nodes                       # nodes intact
    assert set(dst_edges) == set(src_edges)             # edges still present
    assert all(props == () for props in dst_edges.values())  # but property-less


def test_import_uses_the_logged_path(populated):
    """Imported RDF rides service.import_graph, so it is recorded + replayable."""
    nt = rdf.export(populated, "ntriples")
    dst = _fresh_graph()
    log = EventLog(dst)
    summary = rdf.import_rdf(dst, log, nt, fmt="ntriples")
    assert summary["nodes_imported"] == 2
    assert summary["edges_imported"] == 2
    # The import left an audit trail (the whole point of the logged path).
    assert log.count() > 0


def test_round_trip_turtle_via_rdflib(populated):
    pytest.importorskip("rdflib")
    # rdflib's stable parser is RDF 1.1 — no star — so use reification here.
    ctx = rdf.IriContext(edge_strategy="reification")
    ttl = rdf.export(populated, "turtle", ctx)
    dst = _reimport(ttl, "turtle", ctx)
    assert _snapshot(dst) == _snapshot(populated)
