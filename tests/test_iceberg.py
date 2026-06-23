"""Iceberg virtual edges: relationships resolved live from an Apache Iceberg
table in a lakehouse, with DuckDB as the scan engine.

These exercise the real stack — a pyiceberg ``SqlCatalog`` writes an actual
format-version-2 table to a temp warehouse, and the graph resolves edges out of
it through DuckDB on traversal, copying nothing. Skipped cleanly if the optional
``iceberg`` extra (duckdb + pyiceberg + pyarrow) isn't installed.
"""

import pytest

pytest.importorskip("duckdb")
pytest.importorskip("pyiceberg")
pytest.importorskip("pyarrow")

import pyarrow as pa  # noqa: E402
from pyiceberg.catalog.sql import SqlCatalog  # noqa: E402

from kgrdbms import iceberg, virtual  # noqa: E402
from kgrdbms.graph import Graph  # noqa: E402
from kgrdbms.virtual import VirtualEdge  # noqa: E402


def _warehouse(tmp_path, fmt="2"):
    """A local Iceberg lakehouse with one co-holdings table, returning the
    catalog props dict a binding would carry plus the live table handle."""
    wh = tmp_path / "lake"
    wh.mkdir()
    props = {
        "name": "test",
        "uri": f"sqlite:///{wh}/catalog.db",
        "warehouse": f"file://{wh}",
    }
    cat = SqlCatalog(props["name"], **{k: v for k, v in props.items() if k != "name"})
    cat.create_namespace("analytics")
    data = pa.table(
        {
            "a": ["NVDA", "NVDA", "AAPL"],
            "b": ["AMD", "TSM", "MSFT"],
            "shared": [12, 9, 20],
        }
    )
    tbl = cat.create_table(
        "analytics.co_held", schema=data.schema, properties={"format-version": fmt}
    )
    tbl.append(data)
    return props, tbl


def _binding(props):
    return VirtualEdge(
        edge_type="CO_HELD_WITH",
        query="SELECT b AS to_id, shared FROM co_held WHERE a = ?",
        source_type="iceberg",
        catalog=props,
        table="analytics.co_held",
        source="id_slug",  # company:NVDA -> "NVDA"
        target_id_template="company:{value}",
        target_kind="company",
        prop_cols=["shared"],
        directions="both",
    )


def test_resolves_edges_from_iceberg_without_storing(tmp_path):
    props, _ = _warehouse(tmp_path)
    g = Graph(path=tmp_path / "kg.db")
    virtual.register(g, _binding(props))

    # Nothing copied into the graph — the lake stays the single source of truth.
    assert g.total_edges() == 0

    triples = virtual.augment(g, "company:NVDA", "out", None)
    targets = sorted((e.to_node, e.properties["shared"]) for _, e, _ in triples)
    assert targets == [("company:AMD", 12), ("company:TSM", 9)]
    assert all(far.kind == "company" for _, _, far in triples)
    assert all(e.properties["_virtual"] is True for _, e, _ in triples)
    g.close()


def test_multi_table_join(tmp_path):
    """A binding can mount several Iceberg tables and JOIN across them in one edge:
    here a holdings table against a name-lookup dimension."""
    props, _ = _warehouse(tmp_path)
    # Add a lookup table to the same catalog.
    cat = SqlCatalog(props["name"], **{k: v for k, v in props.items() if k != "name"})
    names = pa.table({"b": ["AMD", "TSM", "MSFT"], "long_name": ["Advanced Micro", "Taiwan Semi", "Microsoft"]})
    lk = cat.create_table("analytics.names", schema=names.schema, properties={"format-version": "2"})
    lk.append(names)

    g = Graph(path=tmp_path / "kg.db")
    ve = VirtualEdge(
        edge_type="CO_HELD_WITH",
        query=(
            "SELECT c.b AS to_id, n.long_name, c.shared "
            "FROM co_held c JOIN names n ON c.b = n.b WHERE c.a = ?"
        ),
        source_type="iceberg", catalog=props,
        table="analytics.co_held", tables=["analytics.names"],
        source="id_slug", target_id_template="company:{value}",
        target_kind="company", name_col="long_name", prop_cols=["shared"],
    )
    virtual.register(g, ve)
    out = virtual.augment(g, "company:NVDA", "out", None)
    by_target = {e.to_node: far.name for _, e, far in out}
    assert by_target == {"company:AMD": "Advanced Micro", "company:TSM": "Taiwan Semi"}
    # both tables resolved through iceberg_tables()
    assert ve.iceberg_tables() == ["analytics.co_held", "analytics.names"]
    g.close()


def test_snapshot_pin_rejects_multiple_tables(tmp_path):
    props, _ = _warehouse(tmp_path)
    with pytest.raises(RuntimeError, match="single table"):
        iceberg.open_source(props, ["analytics.co_held", "analytics.other"], snapshot_id=123)


def test_format_version_2(tmp_path):
    _, tbl = _warehouse(tmp_path, fmt="2")
    assert tbl.metadata.format_version == 2


def test_catalog_env_indirection(tmp_path, monkeypatch):
    """A catalog value written `env:VAR` is resolved from the environment, so the
    warehouse URI (and any creds) never sit in the stored binding."""
    props, _ = _warehouse(tmp_path)
    real_uri = props["uri"]
    props["uri"] = "env:KG_ICEBERG_URI"

    monkeypatch.delenv("KG_ICEBERG_URI", raising=False)
    with pytest.raises(RuntimeError, match="KG_ICEBERG_URI"):
        iceberg.resolve_catalog_props(props)

    monkeypatch.setenv("KG_ICEBERG_URI", real_uri)
    resolved = iceberg.resolve_catalog_props(props)
    assert resolved["uri"] == real_uri
    assert "name" not in resolved  # name configures nothing; it's the catalog id

    # And the full resolve path works through the env indirection.
    g = Graph(path=tmp_path / "kg.db")
    virtual.register(g, _binding(props))
    out = virtual.augment(g, "company:NVDA", "out", None)
    assert sorted(e.to_node for _, e, _ in out) == ["company:AMD", "company:TSM"]
    g.close()


def test_snapshot_pinning_time_travels(tmp_path):
    """A pinned `snapshot_id` reads the table as of that version. The first append
    holds 3 rows; a second append adds a row. Pinning the first snapshot must not
    see the later write."""
    props, tbl = _warehouse(tmp_path)
    first_snapshot = tbl.metadata.snapshots[-1].snapshot_id

    tbl.append(pa.table({"a": ["NVDA"], "b": ["INTC"], "shared": [3]}))

    # Unpinned: sees the new INTC edge.
    live = _binding(props)
    g = Graph(path=tmp_path / "kg.db")
    virtual.register(g, live)
    live_targets = sorted(e.to_node for _, e, _ in virtual.augment(g, "company:NVDA", "out", None))
    assert "company:INTC" in live_targets

    # Pinned to the first snapshot: INTC didn't exist yet.
    pinned = _binding(props)
    pinned.snapshot_id = first_snapshot
    pairs = virtual.resolve(pinned, "company:NVDA", None, "out")
    assert "company:INTC" not in [e.to_node for e, _ in pairs]
    assert sorted(e.to_node for e, _ in pairs) == ["company:AMD", "company:TSM"]
    g.close()


def test_binding_roundtrips_through_ontology(tmp_path):
    props, _ = _warehouse(tmp_path)
    g = Graph(path=tmp_path / "kg.db")
    virtual.register(g, _binding(props))

    listed = virtual.list_bindings(g)
    assert len(listed) == 1
    ve = listed[0]
    assert ve.source_type == "iceberg"
    assert ve.table == "analytics.co_held"
    assert ve.catalog["uri"] == props["uri"]
    g.close()


# --- live AWS integration -------------------------------------------------
# Opt-in: set KG_ICEBERG_S3TABLES_ARN to a table-bucket ARN holding the SEC
# form13f namespace (and have AWS creds in the environment). Proves the s3://
# path — DuckDB reading S3 Tables' managed storage via httpfs + credential_chain.
import os  # noqa: E402

_S3TABLES_ARN = os.environ.get("KG_ICEBERG_S3TABLES_ARN")


@pytest.mark.skipif(not _S3TABLES_ARN, reason="set KG_ICEBERG_S3TABLES_ARN to run")
def test_s3tables_live_resolve():
    region = os.environ.get("AWS_REGION", "us-east-1")
    catalog = {
        "name": "s3tables",
        "type": "rest",
        "uri": f"https://s3tables.{region}.amazonaws.com/iceberg",
        "warehouse": _S3TABLES_ARN,
        "rest.sigv4-enabled": "true",
        "rest.signing-name": "s3tables",
        "rest.signing-region": region,
    }
    g = Graph(path="/tmp/kg_s3tables_it.db")
    ve = VirtualEdge(
        edge_type="HOLDS",
        query=(
            "SELECT matched_symbol AS to_id, name_of_issuer, value_normalized "
            "FROM infotable WHERE manager_cik = ? AND matched_symbol IS NOT NULL "
            "ORDER BY value_normalized DESC LIMIT 5"
        ),
        source_type="iceberg", catalog=catalog, table="form13f.infotable",
        source="id_slug", target_id_template="issuer:{value}", target_kind="issuer",
        name_col="name_of_issuer", prop_cols=["value_normalized"],
    )
    virtual.register(g, ve)
    assert g.total_edges() == 0  # nothing copied — resolved live
    # CIK 0000895421 is a large 13F filer with deep holdings.
    out = virtual.augment(g, "manager:0000895421", "out", None)
    assert out, "expected live holdings resolved from S3 Tables"
    assert all(e.properties["_virtual"] is True for _, e, _ in out)
    assert all(far.id.startswith("issuer:") for _, _, far in out)
    g.close()


def test_iceberg_requires_catalog_and_table(tmp_path):
    g = Graph(path=tmp_path / "kg.db")
    ve = VirtualEdge(
        edge_type="X", query="SELECT b AS to_id FROM co_held WHERE a = ?",
        source_type="iceberg", catalog=None, table=None,
    )
    with pytest.raises(RuntimeError, match="requires"):
        virtual.resolve(ve, "company:NVDA", None, "out")
    g.close()
