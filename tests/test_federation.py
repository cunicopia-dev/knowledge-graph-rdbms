"""Federation — multithreaded cross-ontology reads, identity-aware."""

from kgrdbms import backbone, resolver
from kgrdbms.federation import Federation


def _seed(root, *, shared=False):
    if shared:
        resolver.register("alpha", root=root, shared_identity=True)
        resolver.register("beta", root=root, shared_identity=True)
    a = resolver.resolve("alpha", root=root)
    a.backend.add_node("person:ada", kind="Person", name="Ada", labels={"VIP"}, properties={"born": 1815})
    a.backend.add_node("drink:latte", kind="Drink", name="Latte")
    a.backend.close()
    b = resolver.resolve("beta", root=root)
    b.backend.add_node("person:ada", kind="Person", name="Ada L", properties={"died": 1852})
    b.backend.add_node("paper:notes", kind="Paper", name="Notes")
    b.backend.close()


def test_federated_schema_merges_counts(tmp_path):
    root = str(tmp_path)
    _seed(root)
    s = Federation(["alpha", "beta"], root=root).schema()
    assert set(s["ontologies"]) == {"alpha", "beta"}
    assert s["merged"]["kinds"]["Person"] == 2
    assert s["merged"]["kinds"]["Drink"] == 1
    assert s["merged"]["nodes_total"] == 4
    assert set(s["by_ontology"]) == {"alpha", "beta"}


def test_federated_nodes_by_kind_tags_source(tmp_path):
    root = str(tmp_path)
    _seed(root)
    located = Federation(["alpha", "beta"], root=root).nodes_by_kind("Person")
    assert {(l.ontology, l.node.id) for l in located} == {("alpha", "person:ada"), ("beta", "person:ada")}


def test_local_identity_is_not_merged(tmp_path):
    root = str(tmp_path)
    _seed(root)  # default local identity
    fn = Federation(["alpha", "beta"], root=root).node("person:ada")
    assert len(fn.occurrences) == 2
    assert fn.shared == [] and fn.merged is None


def test_shared_identity_merges_across_ontologies(tmp_path):
    root = str(tmp_path)
    _seed(root, shared=True)
    fn = Federation(["alpha", "beta"], root=root).node("person:ada")
    assert set(fn.shared) == {"alpha", "beta"}
    assert fn.merged is not None
    assert fn.merged.properties == {"born": 1815, "died": 1852}
    assert "VIP" in fn.merged.labels


def test_identity_materializes_explicit_same_as_cluster(tmp_path):
    root = str(tmp_path)
    a = resolver.resolve("a", root=root)
    a.backend.add_node("person:ada", kind="Person", name="Ada")
    a.backend.close()
    b = resolver.resolve("b", root=root)
    b.backend.add_node("author:ada", kind="Author", name="Ada L")
    b.backend.close()
    backbone.same_as("a", "person:ada", "b", "author:ada", root=root)
    ident = Federation(["a", "b"], root=root).identity("a", "person:ada")
    assert {(l.ontology, l.node.id) for l in ident} == {("a", "person:ada"), ("b", "author:ada")}


def test_parallel_and_sequential_agree(tmp_path):
    root = str(tmp_path)
    _seed(root)
    par = Federation(["alpha", "beta"], root=root, parallel=True).schema()["merged"]
    seq = Federation(["alpha", "beta"], root=root, parallel=False).schema()["merged"]
    assert par == seq


def test_federation_all_includes_registered(tmp_path):
    root = str(tmp_path)
    _seed(root)
    fed = Federation.all(root=root)
    assert "alpha" in fed.names and "beta" in fed.names
    stats = fed.stats()
    assert stats["nodes_total"] >= 4
