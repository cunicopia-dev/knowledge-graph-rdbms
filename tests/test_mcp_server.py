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


def test_schema_on_empty_graph(mcp_mod):
    s = mcp_mod.kg_schema()
    assert s["nodes_total"] == 0
    assert s["edges_total"] == 0
    assert s["kinds"] == {}


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


def test_find_by_kind_and_label(mcp_mod):
    mcp_mod.kg_node_upsert(id="a:1", kind="A", name="1", labels=["Tagged"])
    mcp_mod.kg_node_upsert(id="a:2", kind="A", name="2")
    mcp_mod.kg_node_upsert(id="b:1", kind="B", name="1", labels=["Tagged"])
    assert len(mcp_mod.kg_find(kind="A")) == 2
    tagged = {r["node"]["id"] for r in mcp_mod.kg_find(label="Tagged")}
    assert tagged == {"a:1", "b:1"}
    # both filters = intersection (kind A AND label Tagged)
    both = mcp_mod.kg_find(kind="A", label="Tagged")
    assert {r["node"]["id"] for r in both} == {"a:1"}


def test_ontology_delete_tool(mcp_mod):
    mcp_mod.kg_ontology_create(name="coffee")
    mcp_mod.kg_node_upsert(id="drink:latte", kind="Drink", name="Latte", ontology="coffee")
    assert any(o["name"] == "coffee" for o in mcp_mod.kg_ontologies_list())
    res = mcp_mod.kg_ontology_delete("coffee")
    assert res["deregistered"] is True
    assert all(o["name"] != "coffee" for o in mcp_mod.kg_ontologies_list())
    assert mcp_mod.kg_ontology_delete("ghost")["deregistered"] is False


def test_cross_ontology_tools(mcp_mod):
    # two ontologies, one node each, queried and linked across the boundary
    mcp_mod.kg_node_upsert(id="drink:latte", kind="Drink", name="Latte", ontology="coffee")
    mcp_mod.kg_node_upsert(id="person:ada", kind="Person", name="Ada", ontology="people")

    # federation folds into the base reads via ontologies=[...]
    sch = mcp_mod.kg_schema(ontologies=["coffee", "people"])
    assert sch["merged"]["kinds"].get("Drink") == 1 and sch["merged"]["kinds"].get("Person") == 1
    found = mcp_mod.kg_find(kind="Person", ontologies=["coffee", "people"])
    assert any(r["ontology"] == "people" and r["node"]["id"] == "person:ada" for r in found)

    mcp_mod.kg_link("coffee", "drink:latte", "ENJOYED_BY", "people", "person:ada",
                    properties={"since": 2020})
    links = mcp_mod.kg_links_of("coffee", "drink:latte")
    assert links[0]["type"] == "ENJOYED_BY" and links[0]["ontology"] == "people"

    # SAME_AS via kg_link(type=SAME_AS, symmetric=True), read back with kg_identity
    mcp_mod.kg_link("coffee", "drink:latte", "SAME_AS", "people", "person:ada", symmetric=True)
    cluster = mcp_mod.kg_identity("coffee", "drink:latte")
    assert {(r["ontology"], r["node"]["id"]) for r in cluster} == {
        ("coffee", "drink:latte"), ("people", "person:ada")}

    mcp_mod.kg_prefix_add("person", "https://kg.local/person/")
    assert mcp_mod.kg_prefix_resolve("person:ada")["iri"] == "https://kg.local/person/ada"
    assert "person" in mcp_mod.kg_prefix_resolve()


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
    out = mcp_mod.kg_edges("x:1", direction="out", edge_type="REL")
    assert out[0]["direction"] == "out" and out[0]["node"]["id"] == "x:2"
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
    assert len(mcp_mod.kg_find(kind="T")) == 1
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
    assert mcp_mod.kg_edges("t:b", direction="in", edge_type="REL") == []


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


# ---- HTTP bearer auth (the _require_bearer ASGI wrapper) --------------
#
# Drives the wrapper as a pure ASGI app (no uvicorn, no sockets): a fake
# downstream records whether it was reached, and a fake send() collects the
# response messages. This is the same wrapper served over streamable-http.

import asyncio


def _drive(app, scope):
    """Run an ASGI app once against a scope; return (downstream_reached, messages)."""
    reached = {"hit": False}

    async def downstream(s, receive, send):
        reached["hit"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    from kgrdbms.mcp_server import _require_bearer

    guarded = _require_bearer(downstream, "s3cret")
    asyncio.run(guarded(scope, receive, send))
    return reached["hit"], sent


def _http_scope(auth: bytes | None):
    headers = [(b"authorization", auth)] if auth is not None else []
    return {"type": "http", "method": "POST", "path": "/mcp", "headers": headers}


def test_bearer_missing_header_is_401(mcp_mod):
    reached, sent = _drive(mcp_mod, _http_scope(None))
    assert reached is False
    assert sent[0]["status"] == 401


def test_bearer_wrong_token_is_401(mcp_mod):
    reached, sent = _drive(mcp_mod, _http_scope(b"Bearer nope"))
    assert reached is False
    assert sent[0]["status"] == 401


def test_bearer_correct_token_passes_through(mcp_mod):
    reached, sent = _drive(mcp_mod, _http_scope(b"Bearer s3cret"))
    assert reached is True
    assert sent[0]["status"] == 200


def test_bearer_lifespan_scope_passes_through(mcp_mod):
    # Non-HTTP scopes (e.g. lifespan) must never be blocked by the auth gate.
    reached, _ = _drive(mcp_mod, {"type": "lifespan"})
    assert reached is True


# ---- DNS-rebinding Host allowlist (the HTTP bind fix) -----------------


def test_allowlist_includes_bind_host_and_localhost(mcp_mod):
    mcp_mod._configure_host_allowlist("100.64.0.1", ["myhost.example:*"])
    ts = mcp_mod.mcp.settings.transport_security
    assert ts.enable_dns_rebinding_protection is True
    assert "100.64.0.1:*" in ts.allowed_hosts        # the bind host
    assert "127.0.0.1:*" in ts.allowed_hosts         # localhost always allowed
    assert "myhost.example:*" in ts.allowed_hosts    # caller-supplied hostname


def test_allowlist_star_disables_protection(mcp_mod):
    mcp_mod._configure_host_allowlist("0.0.0.0", ["*"])
    ts = mcp_mod.mcp.settings.transport_security
    assert ts.enable_dns_rebinding_protection is False
