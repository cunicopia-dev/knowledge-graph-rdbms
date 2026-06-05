"""In-process tests for the MCP server tool surface.

We don't spawn the stdio transport. FastMCP's @mcp.tool() decorator registers
the function with the server but returns the underlying callable, so the same
code that runs over the wire runs under pytest.

Each test reloads mcp_server with KGRDBMS_HOME pointed at a tmp_path so the
module-level Graph singleton lands on a fresh database.
"""

from __future__ import annotations

import importlib
import sys

import pytest

pytest.importorskip("mcp")


@pytest.fixture
def mcp_mod(tmp_path, monkeypatch):
    monkeypatch.setenv("KGRDBMS_HOME", str(tmp_path))
    sys.modules.pop("kgrdbms.mcp_server", None)
    return importlib.import_module("kgrdbms.mcp_server")


# ---- reads -----------------------------------------------------------


def test_stats_on_empty_graph(mcp_mod):
    stats = mcp_mod.kg_stats()
    assert stats["nodes_total"] == 0
    assert stats["edges_total"] == 0
    assert stats["db_path"].endswith("graph.db")


def test_node_get_missing_returns_none(mcp_mod):
    assert mcp_mod.kg_node_get("nope:1") is None


def test_upsert_then_get(mcp_mod):
    mcp_mod.kg_node_upsert(
        id="concept:thing",
        kind="Concept",
        name="thing",
        labels=["Concept", "UserAdded"],
        properties={"weight": 0.42},
    )
    n = mcp_mod.kg_node_get("concept:thing")
    assert n is not None
    assert "UserAdded" in n["labels"]
    assert n["properties"]["weight"] == 0.42


def test_nodes_by_kind_and_label(mcp_mod):
    mcp_mod.kg_node_upsert(id="a:1", kind="A", name="1", labels=["Tagged"])
    mcp_mod.kg_node_upsert(id="a:2", kind="A", name="2")
    mcp_mod.kg_node_upsert(id="b:1", kind="B", name="1", labels=["Tagged"])
    assert len(mcp_mod.kg_nodes_by_kind("A")) == 2
    tagged = {n["id"] for n in mcp_mod.kg_nodes_by_label("Tagged")}
    assert tagged == {"a:1", "b:1"}


def test_federation_and_backbone_tools(mcp_mod):
    # two ontologies, one node each, linked across the boundary
    mcp_mod.kg_node_upsert(id="drink:latte", kind="Drink", name="Latte", ontology="coffee")
    mcp_mod.kg_node_upsert(id="person:ada", kind="Person", name="Ada", ontology="people")
    sch = mcp_mod.kg_federated_schema()
    assert sch["merged"]["kinds"].get("Drink") == 1 and sch["merged"]["kinds"].get("Person") == 1

    by_kind = mcp_mod.kg_federated_nodes_by_kind("Person")
    assert any(r["ontology"] == "people" and r["node"]["id"] == "person:ada" for r in by_kind)

    mcp_mod.kg_link("coffee", "drink:latte", "ENJOYED_BY", "people", "person:ada",
                    properties={"since": 2020})
    links = mcp_mod.kg_links_of("coffee", "drink:latte")
    assert links[0]["type"] == "ENJOYED_BY" and links[0]["ontology"] == "people"

    mcp_mod.kg_same_as("coffee", "drink:latte", "people", "person:ada")
    cluster = mcp_mod.kg_identity("coffee", "drink:latte")
    assert {(r["ontology"], r["node"]["id"]) for r in cluster} == {
        ("coffee", "drink:latte"), ("people", "person:ada")}

    mcp_mod.kg_prefix_add("person", "https://kg.local/person/")
    assert mcp_mod.kg_expand("person:ada")["iri"] == "https://kg.local/person/ada"


def test_schema_exposes_vocabulary(mcp_mod):
    mcp_mod.kg_node_upsert(id="a:1", kind="A", name="1", labels=["Tagged"],
                           properties={"status": "active"})
    mcp_mod.kg_node_upsert(id="a:2", kind="A", name="2", properties={"status": "archived"})
    s = mcp_mod.kg_schema()
    assert s["kinds"] == {"A": 2}
    assert s["labels"] == {"Tagged": 1}
    assert s["node_keys_by_kind"]["A"] == {"status": 2}
    # samples enumerate the enum-like status values
    s2 = mcp_mod.kg_schema(samples=True)
    assert s2["samples"]["A"]["values"]["status"] == ["active", "archived"]


def test_edges_out_and_shortest_path(mcp_mod):
    for nid in ("x:1", "x:2", "x:3"):
        mcp_mod.kg_node_upsert(id=nid, kind="X", name=nid)
    mcp_mod.kg_edge_add(from_id="x:1", to_id="x:2", type="REL")
    mcp_mod.kg_edge_add(from_id="x:2", to_id="x:3", type="REL")
    out = mcp_mod.kg_edges_out("x:1", edge_type="REL")
    assert out[0]["target"]["id"] == "x:2"
    path = mcp_mod.kg_shortest_path("x:1", "x:3")
    assert [n["id"] for n in path] == ["x:1", "x:2", "x:3"]


def test_descendants(mcp_mod):
    for nid in ("n:a", "n:b", "n:c"):
        mcp_mod.kg_node_upsert(id=nid, kind="N", name=nid)
    mcp_mod.kg_edge_add(from_id="n:a", to_id="n:b", type="T")
    mcp_mod.kg_edge_add(from_id="n:b", to_id="n:c", type="T")
    descs = {n["id"] for n in mcp_mod.kg_descendants("n:a", "T")}
    assert descs == {"n:b", "n:c"}


# ---- writes ----------------------------------------------------------


def test_import_bulk_in_one_call(mcp_mod):
    res = mcp_mod.kg_import(
        nodes=[
            {"id": "person:ada", "kind": "Person", "name": "Ada", "labels": ["Person"],
             "properties": {"born": 1815}},
            {"id": "field:cs", "kind": "Field", "name": "CS"},
        ],
        edges=[{"from": "person:ada", "to": "field:cs", "type": "FOUNDED",
                "properties": {"year": 1843}}],
    )
    assert res["nodes_imported"] == 2 and res["edges_imported"] == 1
    assert mcp_mod.kg_node_get("person:ada")["properties"]["born"] == 1815
    assert mcp_mod.kg_shortest_path("person:ada", "field:cs")[-1]["id"] == "field:cs"
    # every imported item was logged (2 upserts + 1 edge)
    assert len(mcp_mod.kg_events_tail(10)) == 3


def test_import_is_idempotent_and_gated(mcp_mod, monkeypatch):
    doc = {"nodes": [{"id": "a:1", "kind": "T", "name": "1"}], "edges": []}
    mcp_mod.kg_import(**doc)
    mcp_mod.kg_import(**doc)  # re-import merges, not duplicates
    assert len(mcp_mod.kg_nodes_by_kind("T")) == 1
    # a denying policy aborts the whole import (atomic) and raises
    from kgrdbms.policy import Decision
    monkeypatch.setattr("kgrdbms.policy.mutation_check", lambda ctx: Decision.deny("sealed"))
    with pytest.raises(PermissionError):
        mcp_mod.kg_import(nodes=[{"id": "b:1", "kind": "T", "name": "x"}])
    assert mcp_mod.kg_node_get("b:1") is None  # nothing written


def test_edge_add_and_remove_roundtrip(mcp_mod):
    mcp_mod.kg_node_upsert(id="x:1", kind="X", name="one")
    mcp_mod.kg_node_upsert(id="x:2", kind="X", name="two")
    edge = mcp_mod.kg_edge_add(from_id="x:1", to_id="x:2", type="REFERENCES")
    assert edge["type"] == "REFERENCES"
    rm = mcp_mod.kg_edge_remove(from_id="x:1", to_id="x:2", type="REFERENCES")
    assert rm["removed"] == 1
    rm2 = mcp_mod.kg_edge_remove(from_id="x:1", to_id="x:2", type="REFERENCES")
    assert rm2["removed"] == 0


def test_node_delete_cascades_edges(mcp_mod):
    mcp_mod.kg_node_upsert(id="t:a", kind="T", name="a")
    mcp_mod.kg_node_upsert(id="t:b", kind="T", name="b")
    mcp_mod.kg_edge_add(from_id="t:a", to_id="t:b", type="REL")
    mcp_mod.kg_node_delete("t:a")
    assert mcp_mod.kg_edges_in("t:b", edge_type="REL") == []


# ---- gate ------------------------------------------------------------


def test_default_policy_is_permissive(mcp_mod):
    mcp_mod.kg_node_upsert(id="ephemeral:1", kind="Ephemeral", name="ghost")
    out = mcp_mod.kg_node_delete("ephemeral:1")
    assert out["deleted"] is True


def test_policy_denial_raises_permission_error(mcp_mod, monkeypatch):
    from kgrdbms.policy import Decision

    # The service gate resolves mutation_check through the policy module, so
    # patching it there affects every write path (MCP and CLI alike).
    monkeypatch.setattr("kgrdbms.policy.mutation_check", lambda ctx: Decision.deny("sealed"))
    with pytest.raises(PermissionError):
        mcp_mod.kg_node_upsert(id="blocked:1", kind="X", name="x")


def test_invariant_violation_runs_before_policy(mcp_mod, monkeypatch):
    from kgrdbms.invariants import InvariantViolation

    def seal(graph, ctx):
        if ctx.operation == "node_delete" and ctx.node_kind == "Root":
            raise InvariantViolation("root is the floor")

    monkeypatch.setattr("kgrdbms.invariants.enforce", seal)
    mcp_mod.kg_node_upsert(id="root:1", kind="Root", name="root")
    with pytest.raises(InvariantViolation):
        mcp_mod.kg_node_delete("root:1")
    assert mcp_mod.kg_node_get("root:1") is not None


# ---- event log -------------------------------------------------------


def test_events_tail_records_writes(mcp_mod):
    mcp_mod.kg_node_upsert(id="e:1", kind="E", name="1")
    ops = [e["op"] for e in mcp_mod.kg_events_tail(10)]
    assert "NODE_UPSERT" in ops


def test_event_revert_undoes_a_write(mcp_mod):
    mcp_mod.kg_node_upsert(id="e:2", kind="E", name="2")
    ev_id = mcp_mod.kg_events_tail(1)[0]["id"]
    mcp_mod.kg_event_revert(ev_id)
    assert mcp_mod.kg_node_get("e:2") is None


def test_replay_rebuilds_projection(mcp_mod):
    mcp_mod.kg_node_upsert(id="e:3", kind="E", name="3")
    report = mcp_mod.kg_replay()
    assert report["events_applied"] >= 1
    assert mcp_mod.kg_node_get("e:3") is not None
