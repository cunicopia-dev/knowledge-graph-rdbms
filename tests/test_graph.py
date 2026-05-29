from kgrdbms.graph import Graph, slug


def test_slug_dedupes_aggressively():
    assert slug("Empty Spectacle") == "empty-spectacle"
    assert slug("empty spectacle!") == "empty-spectacle"
    assert slug("EMPTY    SPECTACLE", prefix="concept") == "concept:empty-spectacle"


def test_add_node_and_edge_idempotent(tmp_path):
    g = Graph(path=tmp_path / "test.db")
    g.add_node("a", kind="Concept", name="A", labels={"Concept"}, properties={"x": 1})
    g.add_node("a", kind="Concept", name="A", labels={"Concept"})  # idempotent
    g.add_node("b", kind="Concept", name="B", labels={"Concept"})
    g.add_edge("a", "b", "LIKES")
    g.add_edge("a", "b", "LIKES")  # triple uniqueness
    assert g.total_nodes() == 2
    assert g.total_edges() == 1
    g.close()


def test_properties_are_json_round_trip(tmp_path):
    g = Graph(path=tmp_path / "p.db")
    g.add_node("x", kind="K", name="X", properties={"list": [1, 2, 3], "obj": {"k": "v"}})
    node = g.node("x")
    assert node.properties["list"] == [1, 2, 3]
    assert node.properties["obj"] == {"k": "v"}
    g.close()


def test_out_in_and_neighborhood(tmp_path):
    g = Graph(path=tmp_path / "n.db")
    for nid in ["a", "b", "c", "d"]:
        g.add_node(nid, kind="K", name=nid)
    g.add_edge("a", "b", "TO")
    g.add_edge("b", "c", "TO")
    g.add_edge("d", "b", "FROM")
    out = [n.id for _, n in g.out("a", "TO")]
    in_b = [n.id for _, n in g.in_("b")]
    assert out == ["b"]
    assert set(in_b) == {"a", "d"}
    nbhd = g.neighborhood("a", depth=2)
    assert {"a", "b", "c", "d"} <= set(nbhd.keys())
    g.close()


def test_shortest_path(tmp_path):
    g = Graph(path=tmp_path / "sp.db")
    for nid in list("abcdef"):
        g.add_node(nid, kind="K", name=nid)
    g.add_edge("a", "b", "X")
    g.add_edge("b", "c", "X")
    g.add_edge("c", "d", "X")
    g.add_edge("a", "e", "Y")
    g.add_edge("e", "d", "Y")
    path = g.shortest_path("a", "d")
    assert path is not None
    ids = [n.id for n in path]
    assert ids[0] == "a" and ids[-1] == "d"
    assert len(ids) <= 4
    g.close()


def test_descendants_recursive(tmp_path):
    g = Graph(path=tmp_path / "r.db")
    for nid in list("abcde"):
        g.add_node(nid, kind="K", name=nid)
    g.add_edge("a", "b", "T")
    g.add_edge("b", "c", "T")
    g.add_edge("c", "d", "T")
    g.add_edge("a", "e", "OTHER")
    descs = {n.id for n in g.descendants("a", "T", max_depth=10)}
    assert descs == {"b", "c", "d"}
    g.close()


def test_delete_node_cascades_edges(tmp_path):
    g = Graph(path=tmp_path / "d.db")
    g.add_node("a", kind="K", name="a")
    g.add_node("b", kind="K", name="b")
    g.add_edge("a", "b", "REL")
    assert g.delete_node("a") is True
    assert g.node("a") is None
    assert g.in_("b", "REL") == []
    assert g.total_edges() == 0
    g.close()


def test_labels_and_property_unset(tmp_path):
    g = Graph(path=tmp_path / "l.db")
    g.add_node("a", kind="K", name="a", labels={"One"}, properties={"k": "v"})
    g.add_label("a", "Two")
    assert g.node("a").labels == {"One", "Two"}
    g.remove_label("a", "One")
    assert g.node("a").labels == {"Two"}
    g.del_property("a", "k")
    assert "k" not in g.node("a").properties
    g.close()
