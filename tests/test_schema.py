"""schema() — the observed TBox an LLM reads before querying, so it never guesses."""

from kgrdbms.graph import Graph, _scalar_samples


def _seed(g: Graph) -> None:
    g.add_node("person:ada", kind="Person", name="Ada Lovelace",
               labels={"Person", "important"}, properties={"role": "analyst", "born": 1815})
    g.add_node("person:alan", kind="Person", name="Alan Turing",
               labels={"Person"}, properties={"role": "logician", "born": 1912})
    g.add_node("memory:m1", kind="Memory", name="note one",
               properties={"content": "a free-text body well over eighty characters long so the "
                                      "schema sampler treats it as prose, not an enumerable value set",
                           "importance": "high"})
    g.add_node("memory:m2", kind="Memory", name="note two",
               properties={"content": "another distinct free-text body, also comfortably past the "
                                      "eighty-character cap that marks a property as un-enumerable prose",
                           "importance": "low"})
    g.add_edge("person:ada", "memory:m1", "WROTE", properties={"year": 1843})


def test_schema_reports_kinds_edge_types_labels(tmp_path):
    g = Graph(path=tmp_path / "s.db")
    _seed(g)
    s = g.schema()
    assert s["nodes_total"] == 4
    assert s["edges_total"] == 1
    assert s["kinds"] == {"Person": 2, "Memory": 2}
    assert s["edge_types"] == {"WROTE": 1}
    assert s["labels"] == {"Person": 2, "important": 1}
    assert s["edge_keys"] == {"year": 1}
    g.close()


def test_schema_property_keys_are_grouped_by_kind(tmp_path):
    g = Graph(path=tmp_path / "k.db")
    _seed(g)
    s = g.schema()
    assert set(s["node_keys_by_kind"]["Person"]) == {"role", "born"}
    assert s["node_keys_by_kind"]["Person"]["role"] == 2
    assert set(s["node_keys_by_kind"]["Memory"]) == {"content", "importance"}
    g.close()


def test_schema_kind_with_no_properties_still_listed(tmp_path):
    g = Graph(path=tmp_path / "np.db")
    g.add_node("tag:x", kind="Tag", name="x")  # no properties at all
    s = g.schema()
    assert s["kinds"] == {"Tag": 1}
    assert s["node_keys_by_kind"].get("Tag", {}) == {}
    g.close()


def test_schema_samples_enumerate_enum_keys_but_not_freetext(tmp_path):
    g = Graph(path=tmp_path / "samp.db")
    _seed(g)
    s = g.schema(samples=True)
    mem = s["samples"]["Memory"]
    # example ids reveal the CURIE convention
    assert mem["example_ids"] == ["memory:m1", "memory:m2"]
    # importance is enum-like → enumerated; content is free-text → omitted
    assert mem["values"]["importance"] == ["high", "low"]
    assert "content" not in mem["values"]
    # numeric enum on Person too
    assert s["samples"]["Person"]["values"]["born"] == [1815, 1912]
    g.close()


def test_schema_samples_respects_sample_limit(tmp_path):
    g = Graph(path=tmp_path / "lim.db")
    for i in range(30):
        g.add_node(f"n:{i}", kind="K", name=str(i), properties={"v": i})
    s = g.schema(samples=True, sample_limit=10)
    # 30 distinct values > limit 10 → not enumerated
    assert "v" not in s["samples"]["K"]["values"]
    g.close()


def test_scalar_samples_helper_rejects_nonscalar_and_longstrings():
    assert _scalar_samples([1, 2, 3]) == [1, 2, 3]
    assert _scalar_samples(["b", "a"]) == ["a", "b"]
    assert _scalar_samples([True, False]) == [False, True]
    assert _scalar_samples([["a", "b"]]) is None       # list value
    assert _scalar_samples([{"k": "v"}]) is None        # object value
    assert _scalar_samples(["x" * 200]) is None         # over-long string
    assert _scalar_samples([]) is None                  # nothing to show


def test_schema_empty_graph(tmp_path):
    g = Graph(path=tmp_path / "empty.db")
    s = g.schema()
    assert s == {
        "nodes_total": 0, "edges_total": 0,
        "kinds": {}, "edge_types": {}, "labels": {},
        "node_keys_by_kind": {}, "edge_keys": {},
    }
    g.close()
