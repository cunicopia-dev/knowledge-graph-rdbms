"""Backbone — cross-ontology links + the prefix registry, both living in the
index graph and written through the gated + logged service path."""

from kgrdbms import backbone, resolver


def test_qualify_unqualify_preserves_curie_colons():
    assert backbone.qualify("coffee", "drink:latte") == "coffee::drink:latte"
    assert backbone.unqualify("coffee::drink:latte") == ("coffee", "drink:latte")


def test_link_and_links_of_both_directions(tmp_path):
    root = str(tmp_path)
    backbone.link("coffee", "drink:latte", "ENJOYED_BY", "people", "person:ada",
                  properties={"since": 2020}, root=root)
    fwd = backbone.links_of("coffee", "drink:latte", root=root)
    assert len(fwd) == 1
    l = fwd[0]
    assert (l.direction, l.type, l.other_ontology, l.other_id) == ("out", "ENJOYED_BY", "people", "person:ada")
    assert l.properties == {"since": 2020}
    back = backbone.links_of("people", "person:ada", root=root)
    assert back[0].direction == "in" and back[0].other_ontology == "coffee"


def test_same_as_cluster_is_transitive(tmp_path):
    root = str(tmp_path)
    backbone.same_as("a", "person:ada", "b", "author:ada", root=root)
    backbone.same_as("b", "author:ada", "c", "contrib:al", root=root)
    cluster = set(backbone.identity_cluster("a", "person:ada", root=root))
    assert cluster == {("a", "person:ada"), ("b", "author:ada"), ("c", "contrib:al")}


def test_unlink_removes_symmetric_link(tmp_path):
    root = str(tmp_path)
    backbone.same_as("a", "x:1", "b", "y:1", root=root)
    assert len(backbone.links_of("a", "x:1", root=root)) == 2  # out + in (symmetric)
    backbone.unlink("a", "x:1", backbone.SAME_AS, "b", "y:1", symmetric=True, root=root)
    assert backbone.links_of("a", "x:1", root=root) == []


def test_prefix_expand_and_contract(tmp_path):
    root = str(tmp_path)
    backbone.register_prefix("person", "https://kg.local/person/", root=root)
    backbone.register_prefix("org", "https://kg.local/org/", root=root)
    assert backbone.expand("person:ada", root=root) == "https://kg.local/person/ada"
    assert backbone.contract("https://kg.local/person/ada", root=root) == "person:ada"
    assert backbone.expand("missing:x", root=root) is None
    assert backbone.contract("https://elsewhere/x", root=root) is None
    assert backbone.prefixes(root=root)["org"] == "https://kg.local/org/"


def test_link_is_logged_and_reversible(tmp_path):
    root = str(tmp_path)
    backbone.link("a", "x:1", "REL", "b", "y:1", root=root)
    idx, events = backbone._open(root)
    try:
        evs = events.tail(10)
        # two Ref upserts + one edge add, all recorded in the index's event log
        assert len(evs) >= 3
    finally:
        idx.close()


def test_links_dont_pollute_ontology_registry(tmp_path):
    root = str(tmp_path)
    backbone.link("a", "x:1", "REL", "b", "y:1", root=root)
    backbone.register_prefix("p", "https://x/", root=root)
    # Ref/Prefix nodes share the index with Ontology nodes but never appear as ontologies
    assert resolver.list_ontologies(root) == []
