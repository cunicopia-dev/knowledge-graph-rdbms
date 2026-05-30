"""Batched writes, bulk insert helpers, and bulk-hydration parity."""

from __future__ import annotations

import pytest

from kgrdbms.graph import Edge, Graph, Node


# ---- batch() context -------------------------------------------------


def test_batch_defers_commit_and_lands_all(tmp_path):
    g = Graph(path=tmp_path / "b.db")
    with g.batch():
        for i in range(100):
            g.add_node(f"n:{i}", kind="K", name=str(i))
            assert g._batch_depth == 1
    assert g._batch_depth == 0
    assert g.total_nodes() == 100
    g.close()


def test_batch_rolls_back_on_exception(tmp_path):
    g = Graph(path=tmp_path / "b.db")
    g.add_node("keep:1", kind="K", name="keep")
    with pytest.raises(RuntimeError):
        with g.batch():
            g.add_node("doomed:1", kind="K", name="doomed")
            raise RuntimeError("boom")
    # the pre-existing node survives; the in-batch write was rolled back
    assert g.node("keep:1") is not None
    assert g.node("doomed:1") is None
    assert g._batch_depth == 0
    g.close()


def test_batch_nests(tmp_path):
    g = Graph(path=tmp_path / "b.db")
    with g.batch():
        g.add_node("a", kind="K", name="a")
        with g.batch():
            g.add_node("b", kind="K", name="b")
        # inner exit must NOT have committed/closed the outer batch
        assert g._batch_depth == 1
    assert g._batch_depth == 0
    assert g.total_nodes() == 2
    g.close()


# ---- add_nodes / add_edges parity ------------------------------------


def test_add_nodes_matches_add_node_semantics(tmp_path):
    single = Graph(path=tmp_path / "single.db")
    bulk = Graph(path=tmp_path / "bulk.db")

    specs = [
        {"id": f"n:{i}", "kind": "K", "name": f"node {i}",
         "labels": ["Even"] if i % 2 == 0 else ["Odd"],
         "properties": {"i": i, "obj": {"k": i}}}
        for i in range(500)
    ]
    for s in specs:
        single.add_node(s["id"], s["kind"], s["name"], labels=s["labels"], properties=s["properties"])
    assert bulk.add_nodes(specs) == 500

    for i in (0, 1, 250, 499):
        a = single.node(f"n:{i}")
        b = bulk.node(f"n:{i}")
        assert a.kind == b.kind and a.name == b.name
        assert a.labels == b.labels
        assert a.properties == b.properties
    assert single.count_nodes_by_kind() == bulk.count_nodes_by_kind()
    single.close(); bulk.close()


def test_add_nodes_accepts_node_objects_and_is_idempotent(tmp_path):
    g = Graph(path=tmp_path / "g.db")
    g.add_nodes([Node(id="x", kind="K", name="X", labels={"L"}, properties={"p": 1})])
    # re-upsert with new label + changed property; id stays single, labels accumulate
    g.add_nodes([{"id": "x", "kind": "K", "name": "X2", "labels": ["M"], "properties": {"p": 2}}])
    n = g.node("x")
    assert g.total_nodes() == 1
    assert n.name == "X2"
    assert n.labels == {"L", "M"}
    assert n.properties["p"] == 2
    g.close()


def test_add_edges_dedupes_triples_and_sets_props(tmp_path):
    g = Graph(path=tmp_path / "e.db")
    g.add_nodes([{"id": "a", "kind": "K"}, {"id": "b", "kind": "K"}, {"id": "c", "kind": "K"}])
    n = g.add_edges([
        ("a", "b", "REL"),
        ("a", "b", "REL"),  # duplicate triple — must collapse
        {"from": "a", "to": "c", "type": "REL", "properties": {"w": 0.5}},
        Edge(id="ignored", from_node="b", to_node="c", type="LINK", properties={"k": "v"}),
    ])
    assert n == 4
    assert g.total_edges() == 3  # the duplicate (a,REL,b) collapsed
    # properties landed on the right edges
    ac = [e for e, _ in g.out("a", "REL") if e.to_node == "c"][0]
    assert ac.properties == {"w": 0.5}
    bc = g.out("b", "LINK")[0][0]
    assert bc.properties == {"k": "v"}
    g.close()


def test_add_edges_property_upsert_on_existing_triple(tmp_path):
    g = Graph(path=tmp_path / "e2.db")
    g.add_nodes([{"id": "a", "kind": "K"}, {"id": "b", "kind": "K"}])
    g.add_edge("a", "b", "REL", properties={"w": 1})
    g.add_edges([{"from": "a", "to": "b", "type": "REL", "properties": {"w": 2, "extra": True}}])
    assert g.total_edges() == 1
    e = g.out("a", "REL")[0][0]
    assert e.properties == {"w": 2, "extra": True}
    g.close()


# ---- bulk-hydration parity -------------------------------------------


def test_bulk_hydration_matches_single_hydration(tmp_path):
    g = Graph(path=tmp_path / "h.db")
    with g.batch():
        for i in range(300):
            g.add_node(f"n:{i}", kind="K", name=f"node {i}",
                       labels={"Tagged"} if i % 3 == 0 else set(),
                       properties={"i": i, "nested": [i, i + 1]})

    bulk = g.nodes_by_kind("K")
    # reconstruct the single-hydration view for comparison
    single = {n.id: g._hydrate_node(
        g.conn.execute("SELECT * FROM nodes WHERE id=?", (n.id,)).fetchone()
    ) for n in bulk}

    assert len(bulk) == 300
    for n in bulk:
        s = single[n.id]
        assert n.labels == s.labels
        assert n.properties == s.properties
    # label query returns exactly the tagged set
    tagged = g.nodes_by_label("Tagged")
    assert len(tagged) == 100
    assert all("Tagged" in n.labels for n in tagged)
    g.close()


def test_out_in_edge_properties_survive_bulk_hydration(tmp_path):
    g = Graph(path=tmp_path / "ep.db")
    g.add_nodes([{"id": "a", "kind": "K"}, {"id": "b", "kind": "K"}])
    g.add_edge("a", "b", "REL", properties={"weight": 0.9, "note": "hi"})
    edge, target = g.out("a", "REL")[0]
    assert target.id == "b"
    assert edge.properties == {"weight": 0.9, "note": "hi"}
    iedge, source = g.in_("b", "REL")[0]
    assert source.id == "a"
    assert iedge.properties == {"weight": 0.9, "note": "hi"}
    g.close()


def test_neighborhood_still_correct_after_refactor(tmp_path):
    g = Graph(path=tmp_path / "nb.db")
    g.add_nodes([{"id": x, "kind": "K"} for x in ("a", "b", "c", "d", "e")])
    g.add_edges([("a", "b", "T"), ("b", "c", "T"), ("d", "b", "T")])
    nb1 = g.neighborhood("a", depth=1)
    assert set(nb1) == {"a", "b"}
    nb2 = g.neighborhood("a", depth=2)
    assert {"a", "b", "c", "d"} <= set(nb2)
    assert "e" not in nb2
    # missing root -> empty
    assert g.neighborhood("nope") == {}
    g.close()
