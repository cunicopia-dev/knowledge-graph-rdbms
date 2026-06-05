"""CLI tests: invoke main(argv) directly, assert on output, exit codes, and
the resulting graph state. Writes must be gated AND logged."""

from __future__ import annotations

import json

import pytest

from kgrdbms.cli import main
from kgrdbms.graph import Graph


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "cli.db")


def run(db, *argv, as_json=False):
    args = ["--db", db]
    if as_json:
        args.append("--json")
    return main(args + list(argv))


# ---- basic round trips ----------------------------------------------


def test_node_add_then_get(db, capsys):
    assert run(db, "node", "add", "person:ada", "--kind", "Person", "--name", "Ada") == 0
    capsys.readouterr()
    assert run(db, "node", "get", "person:ada") == 0
    out = capsys.readouterr().out
    assert "person:ada" in out and "Ada" in out


def test_prop_values_parse_as_json_then_string(db, capsys):
    run(db, "node", "add", "n:1", "--kind", "K",
        "--prop", "born=1815", "--prop", "ok=true",
        "--prop", "tags=[\"a\",\"b\"]", "--prop", "name=Ada")
    run(db, "node", "get", "n:1", )  # ensure committed
    capsys.readouterr()
    assert run(db, "node", "get", "n:1", as_json=True) == 0
    payload = json.loads(capsys.readouterr().out)
    props = payload["properties"]
    assert props["born"] == 1815 and isinstance(props["born"], int)
    assert props["ok"] is True
    assert props["tags"] == ["a", "b"]
    assert props["name"] == "Ada"  # not valid JSON -> kept as string


def test_missing_node_get_exits_1(db, capsys):
    assert run(db, "node", "get", "nope") == 1


# ---- writes are logged (the whole reason for log+gate) ---------------


def test_writes_are_logged_and_survive_replay(db, capsys):
    run(db, "node", "add", "a", "--kind", "K")
    run(db, "node", "add", "b", "--kind", "K")
    run(db, "edge", "add", "a", "b", "REL")
    capsys.readouterr()

    # the log has the three mutations
    assert run(db, "events", "-n", "10", as_json=True) == 0
    ops = [e["op"] for e in json.loads(capsys.readouterr().out)]
    assert ops == ["NODE_UPSERT", "NODE_UPSERT", "EDGE_ADD"]

    # replay rebuilds purely from the log; logged writes survive
    assert run(db, "replay") == 0
    capsys.readouterr()
    g = Graph(path=db)
    assert g.node("a") is not None and g.node("b") is not None
    assert g.out("a", "REL")
    g.close()


def test_revert_undoes_a_logged_write(db, capsys):
    run(db, "node", "add", "tmp", "--kind", "K")
    capsys.readouterr()
    run(db, "events", "-n", "1", as_json=True)
    ev_id = json.loads(capsys.readouterr().out)[0]["id"]
    assert run(db, "revert", ev_id) == 0
    g = Graph(path=db)
    assert g.node("tmp") is None
    g.close()


# ---- edges / traversal ----------------------------------------------


def test_edge_add_rm_and_path(db, capsys):
    run(db, "node", "add", "x", "--kind", "K")
    run(db, "node", "add", "y", "--kind", "K")
    run(db, "edge", "add", "x", "y", "REL")
    capsys.readouterr()
    assert run(db, "path", "x", "y") == 0
    assert "x" in capsys.readouterr().out
    assert run(db, "edge", "rm", "x", "y", "REL") == 0
    assert run(db, "edge", "rm", "x", "y", "REL") == 1  # idempotent: nothing removed


# ---- import ----------------------------------------------------------


def test_import_is_gated_logged_and_replayable(db, tmp_path, capsys):
    doc = {
        "nodes": [{"id": "x:1", "kind": "X", "labels": ["L"]}, {"id": "x:2", "kind": "X"}],
        "edges": [{"from": "x:1", "to": "x:2", "type": "REL", "properties": {"w": 0.5}}],
    }
    f = tmp_path / "imp.json"
    f.write_text(json.dumps(doc))
    assert run(db, "import", str(f)) == 0
    capsys.readouterr()

    g = Graph(path=db)
    assert g.total_nodes() == 2 and g.total_edges() == 1
    g.close()

    # imported writes were logged -> they survive a replay
    assert run(db, "replay") == 0
    capsys.readouterr()
    g = Graph(path=db)
    assert g.out("x:1", "REL")[0][0].properties == {"w": 0.5}
    g.close()


# ---- gate surfaces cleanly -------------------------------------------


def test_policy_denial_exits_2(db, capsys, monkeypatch):
    from kgrdbms.policy import Decision

    monkeypatch.setattr("kgrdbms.policy.mutation_check", lambda ctx: Decision.deny("nope"))
    assert run(db, "node", "add", "blocked", "--kind", "K") == 2
    assert "policy" in capsys.readouterr().err


def test_invariant_violation_exits_3(db, capsys, monkeypatch):
    from kgrdbms.invariants import InvariantViolation

    def seal(graph, ctx):
        if ctx.operation == "node_upsert" and ctx.node_kind == "Sealed":
            raise InvariantViolation("sealed kind")

    monkeypatch.setattr("kgrdbms.invariants.enforce", seal)
    assert run(db, "node", "add", "x", "--kind", "Sealed") == 3
    assert "invariant" in capsys.readouterr().err


# ---- rdf export / import --------------------------------------------


def _seed_pair(db):
    run(db, "node", "add", "person:ada", "--kind", "Person", "--name", "Ada", "--prop", "born=1815")
    run(db, "node", "add", "person:grace", "--kind", "Person", "--name", "Grace")
    run(db, "edge", "add", "person:ada", "person:grace", "influences",
        "--prop", "since=2020", "--prop", "weight=0.8")


def test_rdf_export_turtle_star(db, capsys):
    _seed_pair(db)
    capsys.readouterr()
    assert run(db, "rdf", "export", "--format", "turtle") == 0
    out = capsys.readouterr().out
    assert "person:ada a kg:Person ;" in out                  # CURIE round-trips
    assert "<< person:ada rel:influences person:grace >>" in out  # rdf-star edge


def test_rdf_round_trip_ntriples(db, tmp_path, capsys):
    _seed_pair(db)
    path = str(tmp_path / "g.nt")
    assert run(db, "rdf", "export", "--format", "ntriples", "--out", path) == 0
    mirror = str(tmp_path / "mirror.db")
    assert run(mirror, "rdf", "import", path, "--format", "ntriples") == 0
    # The edge and its property survived into the fresh db.
    g = Graph(path=mirror)
    edge_props = {(e.type): e.properties for e, _ in g.out("person:ada")}
    assert edge_props["influences"]["since"] == 2020
    assert edge_props["influences"]["weight"] == 0.8


def test_rdf_export_lossy_reports_dropped(db, capsys):
    _seed_pair(db)
    capsys.readouterr()
    assert run(db, "rdf", "export", "--format", "ntriples", "--edge-strategy", "lossy") == 0
    captured = capsys.readouterr()
    assert "rel/influences" in captured.out      # bare edge present
    assert "prop/since" not in captured.out       # property dropped
    assert "dropped" in captured.err              # but loudly, not silently


# ---- regression: FK violations exit 1 cleanly, no traceback ---------


def test_set_label_missing_node_exits_1(db, capsys):
    assert run(db, "node", "add-label", "ghost:1", "L") == 1
    err = capsys.readouterr().err
    assert "does not exist" in err and "Traceback" not in err


def test_set_prop_missing_node_exits_1(db, capsys):
    assert run(db, "node", "set-prop", "ghost:1", "k", "1") == 1
    assert "does not exist" in capsys.readouterr().err


def test_edge_add_missing_endpoint_exits_1(db, capsys):
    run(db, "node", "add", "x:1", "--kind", "T")
    capsys.readouterr()
    assert run(db, "edge", "add", "x:1", "y:1", "LINK") == 1
    err = capsys.readouterr().err
    assert "to node 'y:1' does not exist" in err and "Traceback" not in err


def test_schema_json_lists_kinds_and_keys(db, capsys):
    run(db, "node", "add", "person:ada", "--kind", "Person", "--prop", "role=analyst")
    run(db, "node", "add", "memory:m1", "--kind", "Memory", "--prop", "importance=high")
    capsys.readouterr()
    assert run(db, "schema", as_json=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kinds"] == {"Person": 1, "Memory": 1}
    assert payload["node_keys_by_kind"]["Person"] == {"role": 1}
    assert payload["node_keys_by_kind"]["Memory"] == {"importance": 1}


def test_schema_samples_human_shows_enum_values(db, capsys):
    run(db, "node", "add", "memory:m1", "--kind", "Memory", "--prop", "importance=high")
    run(db, "node", "add", "memory:m2", "--kind", "Memory", "--prop", "importance=low")
    capsys.readouterr()
    assert run(db, "schema", "--samples") == 0
    out = capsys.readouterr().out
    assert "importance" in out and "high" in out and "low" in out


def test_federation_link_prefix_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KGRDBMS_HOME", str(tmp_path))
    assert main(["ontology", "create", "coffee"]) == 0
    assert main(["ontology", "create", "people", "--shared-identity"]) == 0
    assert main(["--ontology", "coffee", "node", "add", "drink:latte", "--kind", "Drink"]) == 0
    assert main(["--ontology", "people", "node", "add", "person:ada", "--kind", "Person"]) == 0
    capsys.readouterr()

    assert main(["link", "add", "coffee", "drink:latte", "ENJOYED_BY", "people", "person:ada"]) == 0
    assert "ENJOYED_BY" in capsys.readouterr().out

    assert main(["--json", "fed", "schema"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["merged"]["kinds"]["Drink"] == 1 and payload["merged"]["kinds"]["Person"] == 1

    assert main(["--json", "link", "of", "coffee", "drink:latte"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["ontology"] == "people"

    assert main(["prefix", "add", "person", "https://kg.local/person/"]) == 0
    capsys.readouterr()
    assert main(["prefix", "expand", "person:ada"]) == 0
    assert "https://kg.local/person/ada" in capsys.readouterr().out
