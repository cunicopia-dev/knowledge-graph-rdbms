"""Virtual edges: relationships resolved live from an external SQL source.

The 'external source' here is a separate sqlite file standing in for the
operational store (Postgres in production). The point is to prove the graph
synthesizes edges from it on traversal without ever copying rows in.
"""

import os
import sqlite3

import pytest

from kgrdbms import virtual
from kgrdbms.graph import Graph
from kgrdbms.virtual import VirtualEdge


def _external_source(path):
    """A stand-in operational table: company co-holdings with a weight."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE co_held (a TEXT, b TEXT, shared INTEGER)")
    conn.executemany(
        "INSERT INTO co_held VALUES (?, ?, ?)",
        [("NVDA", "AMD", 12), ("NVDA", "TSM", 9), ("AAPL", "MSFT", 20)],
    )
    conn.commit()
    conn.close()
    return path


def _binding(dsn):
    return VirtualEdge(
        edge_type="CO_HELD_WITH",
        query="SELECT b AS to_id, shared FROM co_held WHERE a = ?",
        dsn=str(dsn),
        source="id_slug",  # company:NVDA -> "NVDA"
        target_col="to_id",
        target_id_template="company:{value}",
        target_kind="company",
        prop_cols=["shared"],
        directions="both",
    )


def test_resolve_synthesizes_edges_without_storing(tmp_path):
    src = _external_source(tmp_path / "ext.db")
    g = Graph(path=tmp_path / "kg.db")
    virtual.register(g, _binding(src))

    # Nothing was stored as a real edge — the binding is the only addition.
    assert g.total_edges() == 0

    triples = virtual.augment(g, "company:NVDA", "out", None)
    targets = sorted((e.to_node, e.properties["shared"]) for _, e, _ in triples)
    assert targets == [("company:AMD", 12), ("company:TSM", 9)]
    # far-end nodes are synthesized stubs, and edges are flagged virtual
    assert all(far.kind == "company" for _, _, far in triples)
    assert all(e.properties["_virtual"] is True for _, e, _ in triples)
    g.close()


def test_edge_type_filter_and_direction(tmp_path):
    src = _external_source(tmp_path / "ext.db")
    g = Graph(path=tmp_path / "kg.db")
    virtual.register(g, _binding(src))

    # Non-matching edge type resolves nothing.
    assert virtual.augment(g, "company:NVDA", "out", "OWNS") == []
    # "both" with a both-binding yields out and in orientations.
    both = virtual.augment(g, "company:NVDA", "both", "CO_HELD_WITH")
    dirs = sorted(d for d, _, _ in both)
    assert dirs == ["in", "in", "out", "out"]
    # an "in" edge is re-oriented to point at the traversed node
    in_edges = [e for d, e, _ in both if d == "in"]
    assert all(e.to_node == "company:NVDA" for e in in_edges)
    g.close()


def test_prop_source_requires_node(tmp_path):
    src = _external_source(tmp_path / "ext.db")
    g = Graph(path=tmp_path / "kg.db")
    ve = _binding(src)
    ve.source = "prop:ticker"
    virtual.register(g, ve)

    # No node / no property -> nothing to bind, resolves empty (no crash).
    assert virtual.augment(g, "company:NVDA", "out", None) == []
    # With a real node carrying the ticker property, it resolves.
    g.add_node("company:NVDA", kind="company", name="NVIDIA", properties={"ticker": "NVDA"})
    out = virtual.augment(g, "company:NVDA", "out", None)
    assert sorted(e.to_node for _, e, _ in out) == ["company:AMD", "company:TSM"]
    g.close()


def test_dsn_env_resolution(tmp_path, monkeypatch):
    src = _external_source(tmp_path / "ext.db")
    ve = VirtualEdge(
        edge_type="CO_HELD_WITH",
        query="SELECT b AS to_id FROM co_held WHERE a = ?",
        dsn_env="TEST_VE_DSN",
        source="id_slug",
        target_id_template="company:{value}",
    )
    # Unset env var -> clear error, not a silent miss.
    monkeypatch.delenv("TEST_VE_DSN", raising=False)
    with pytest.raises(RuntimeError, match="TEST_VE_DSN"):
        virtual.resolve(ve, "company:NVDA", None, "out")
    # Set it and resolution works.
    monkeypatch.setenv("TEST_VE_DSN", str(src))
    pairs = virtual.resolve(ve, "company:NVDA", None, "out")
    assert sorted(e.to_node for e, _ in pairs) == ["company:AMD", "company:TSM"]


def test_binding_roundtrips_and_unregisters(tmp_path):
    src = _external_source(tmp_path / "ext.db")
    g = Graph(path=tmp_path / "kg.db")
    virtual.register(g, _binding(src))

    listed = virtual.list_bindings(g)
    assert [ve.edge_type for ve in listed] == ["CO_HELD_WITH"]
    assert listed[0].directions == "both" and listed[0].prop_cols == ["shared"]

    assert virtual.unregister(g, "CO_HELD_WITH") is True
    assert virtual.list_bindings(g) == []
    g.close()


# Live-Postgres regression: resolve() does dict(row) on every result, so the
# postgres path MUST use a dict row factory — with psycopg's default tuple rows
# dict(row) raises. This is the bug that shipped in 0.1.6 (the sqlite stand-in
# tests above never exercised the postgres branch). Gated: set KG_TEST_PG_DSN
# (a throwaway database) to run it.
_PG_DSN = os.environ.get("KG_TEST_PG_DSN")


@pytest.mark.skipif(not _PG_DSN, reason="set KG_TEST_PG_DSN to run the live-postgres virtual-edge test")
def test_virtual_resolves_against_postgres(tmp_path):
    psycopg = pytest.importorskip("psycopg")
    table = "kg_ve_regression"
    with psycopg.connect(_PG_DSN, autocommit=True) as c:
        c.execute(f"DROP TABLE IF EXISTS {table}")
        c.execute(f"CREATE TABLE {table} (a text, b text, shared int)")
        c.cursor().executemany(
            f"INSERT INTO {table} VALUES (%s,%s,%s)", [("NVDA", "AMD", 12), ("NVDA", "TSM", 9)]
        )
    try:
        g = Graph(path=tmp_path / "kg.db")
        virtual.register(g, VirtualEdge(
            edge_type="CO_HELD_WITH", source="id_slug",
            query=f"SELECT b AS to_id, shared FROM {table} WHERE a = %s",
            dsn=_PG_DSN, target_id_template="company:{value}", prop_cols=["shared"],
        ))
        out = virtual.augment(g, "company:NVDA", "out", None)
        assert sorted((e.to_node, e.properties["shared"]) for _d, e, _f in out) \
            == [("company:AMD", 12), ("company:TSM", 9)]
        g.close()
    finally:
        with psycopg.connect(_PG_DSN, autocommit=True) as c:
            c.execute(f"DROP TABLE IF EXISTS {table}")
