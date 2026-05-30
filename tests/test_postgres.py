"""Postgres backend tests — skipped unless a Postgres is reachable.

Bring one up with:
    docker run -d --name kgrdbms-pg -e POSTGRES_USER=kg -e POSTGRES_PASSWORD=kg \
        -e POSTGRES_DB=kg -p 55432:5432 postgres:16-alpine

Override the DSN with KGRDBMS_TEST_PG_DSN. These exercise the full path the
control plane uses: a postgres-backed ontology whose event log lives in a
control-plane SQLite store, written through the gated `service` layer, with
compensation and replay applied back against the Postgres projection.
"""

from __future__ import annotations

import os

import pytest

psycopg = pytest.importorskip("psycopg")

from kgrdbms import resolver as R, service  # noqa: E402

DSN = os.environ.get("KGRDBMS_TEST_PG_DSN", "postgresql://kg:kg@localhost:55432/kg")


def _pg_reachable() -> bool:
    try:
        with psycopg.connect(DSN, connect_timeout=2):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _pg_reachable(), reason=f"no Postgres at {DSN}")


@pytest.fixture
def pg(tmp_path, monkeypatch):
    """A fresh postgres-backed ontology, isolated index/log under tmp_path."""
    from kgrdbms.backends.postgres import PostgresGraph

    PostgresGraph(DSN).clear()  # wipe the shared test database
    monkeypatch.setenv("KGRDBMS_HOME", str(tmp_path))
    R.register("pg", backend="postgres", path=DSN, stance="literal", root=str(tmp_path))
    resolved = R.resolve("pg", root=str(tmp_path))
    yield resolved
    resolved.backend.close()


def test_backend_routes_to_postgres_with_sqlite_log(pg):
    assert type(pg.backend).__name__ == "PostgresGraph"
    # store (log) is control-plane sqlite; projection is postgres
    assert type(pg.events.store).__name__ == "_ControlPlaneLogStore"
    assert pg.events.projection is pg.backend


def test_crud_and_jsonb_round_trip(pg):
    service.upsert_node(pg.backend, pg.events, id="company:acme", kind="Company", name="Acme",
                        labels=["Company"], properties={"employees": 10, "public": False,
                                                         "tags": ["a", "b"]}, actor="t")
    n = pg.backend.node("company:acme")
    assert n.kind == "Company" and n.labels == {"Company"}
    # jsonb preserves types, not just strings
    assert n.properties == {"employees": 10, "public": False, "tags": ["a", "b"]}
    assert isinstance(n.properties["employees"], int)
    assert isinstance(n.properties["public"], bool)


def test_edge_idempotency_and_traversal(pg):
    service.upsert_node(pg.backend, pg.events, id="a:1", kind="T", name="1", actor="t")
    service.upsert_node(pg.backend, pg.events, id="a:2", kind="T", name="2", actor="t")
    service.upsert_node(pg.backend, pg.events, id="a:3", kind="T", name="3", actor="t")
    service.add_edge(pg.backend, pg.events, "a:1", "a:2", "NEXT", actor="t")
    service.add_edge(pg.backend, pg.events, "a:2", "a:3", "NEXT", actor="t")
    # re-adding the same triple updates, never duplicates
    service.add_edge(pg.backend, pg.events, "a:1", "a:2", "NEXT", properties={"w": 5}, actor="t")
    assert pg.backend.total_edges() == 2

    # recursive-CTE descendants
    assert [d.id for d in pg.backend.descendants("a:1", "NEXT")] == ["a:2", "a:3"]
    # BFS shortest path
    assert [p.id for p in pg.backend.shortest_path("a:1", "a:3")] == ["a:1", "a:2", "a:3"]


def test_compensation_applies_to_postgres(pg):
    service.upsert_node(pg.backend, pg.events, id="x:1", kind="T", name="1", actor="t")
    service.upsert_node(pg.backend, pg.events, id="x:2", kind="T", name="2", actor="t")
    service.add_edge(pg.backend, pg.events, "x:1", "x:2", "LINK", actor="t")
    assert pg.backend.total_edges() == 1

    edge_ev = [e for e in pg.events.tail(10) if e.op == "EDGE_ADD"][0]
    service.revert_event(pg.events, edge_ev.id, actor="op")
    assert pg.backend.total_edges() == 0  # inverse event removed the row from postgres


def test_bulk_add_nodes_and_edges(pg):
    g = pg.backend
    n = g.add_nodes([
        {"id": "b:1", "kind": "T", "name": "1", "labels": ["X"], "properties": {"i": 1}},
        {"id": "b:2", "kind": "T", "name": "2"},
        {"id": "b:1", "kind": "T", "name": "1 again"},  # upsert, not duplicate
    ])
    assert n == 3 and g.total_nodes() == 2
    assert g.node("b:1").name == "1 again" and g.node("b:1").properties == {"i": 1}
    e = g.add_edges([("b:1", "b:2", "REL"), ("b:1", "b:2", "REL", {"w": 9})])
    assert e == 2 and g.total_edges() == 1  # same triple collapses
    assert g.out("b:1")[0][0].properties == {"w": 9}


def test_replay_rebuilds_postgres_from_sqlite_log(pg):
    service.upsert_node(pg.backend, pg.events, id="p:1", kind="Person", name="One", actor="t")
    service.upsert_node(pg.backend, pg.events, id="p:2", kind="Person", name="Two", actor="t")
    service.add_edge(pg.backend, pg.events, "p:1", "p:2", "KNOWS", actor="t")

    report = service.replay_log(pg.backend, pg.events)
    assert report["events_applied"] == 3
    assert pg.backend.total_nodes() == 2
    assert pg.backend.total_edges() == 1
    assert pg.backend.node("p:1") is not None
