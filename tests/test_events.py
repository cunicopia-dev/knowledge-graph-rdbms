"""Event log: append-only, compensation-as-event, and replay/projection."""

from __future__ import annotations

import time

import pytest

from kgrdbms.events import (
    OP_BATCH,
    OP_EDGE_ADD,
    OP_NODE_UPSERT,
    EventLog,
    apply_event,
    replay,
)
from kgrdbms.graph import Graph


def _fresh(tmp_path):
    g = Graph(path=tmp_path / "graph.db")
    log = EventLog(g)
    return g, log


def _node_payload(nid, kind="Concept", name=None, labels=None, props=None):
    return {
        "after": {
            "id": nid,
            "kind": kind,
            "name": name or nid,
            "labels": labels or [],
            "properties": props or {},
        },
        "prior": None,
    }


def test_record_and_tail_are_ordered(tmp_path):
    g, log = _fresh(tmp_path)
    log.record("a", OP_NODE_UPSERT, _node_payload("x:1", "X", "1"))
    log.record("b", OP_NODE_UPSERT, _node_payload("x:2", "X", "2"))
    tail = log.tail(10)
    assert [e.actor for e in tail] == ["a", "b"]
    assert tail[0].seq < tail[1].seq
    g.close()


def test_append_only_compensation_keeps_both_rows(tmp_path):
    g, log = _fresh(tmp_path)
    ev = log.record("alice", OP_NODE_UPSERT, _node_payload("concept:temp", labels=["Concept"]))
    apply_event(g, ev)
    assert g.node("concept:temp") is not None

    before = log.count()
    comp = log.compensate(ev.id, actor="operator")
    after = log.count()

    # the log GREW (append-only); nothing was deleted
    assert after == before + 1
    # the compensation undid the node in the projection
    assert g.node("concept:temp") is None
    # original is marked reverted; compensation points back
    assert log.get(ev.id).reverted_by == comp.id
    assert comp.compensates == ev.id
    # double-revert is refused
    with pytest.raises(ValueError):
        log.compensate(ev.id)
    g.close()


def test_edge_add_compensation_removes_edge(tmp_path):
    g, log = _fresh(tmp_path)
    g.add_node("x:1", "X", "1")
    g.add_node("x:2", "X", "2")
    ev = log.record("p", OP_EDGE_ADD, {"edge": {"from": "x:1", "to": "x:2", "type": "REL", "properties": {}}})
    apply_event(g, ev)
    assert g.out("x:1", "REL")

    log.compensate(ev.id)
    assert g.out("x:1", "REL") == []
    g.close()


def test_replay_reconstructs_state(tmp_path):
    """Replayed events must reproduce the same projection, deterministically."""
    g, log = _fresh(tmp_path)
    for i in (1, 2):
        ev = log.record("p", OP_NODE_UPSERT, _node_payload(f"concept:added-{i}", labels=["Concept"]))
        apply_event(g, ev)

    nodes_before = g.total_nodes()
    assert g.node("concept:added-1") is not None

    report = replay(g, log)
    assert report["events_applied"] == 2
    assert g.total_nodes() == nodes_before
    assert g.node("concept:added-1") is not None
    assert g.node("concept:added-2") is not None
    g.close()


def test_replay_with_genesis_seeds_before_events(tmp_path):
    """A genesis callable re-seeds deterministic state before logged deltas."""
    g, log = _fresh(tmp_path)

    def genesis(graph):
        graph.add_node("seed:root", "Root", "root")

    # The seed node is genesis state; create it first so the edge event applies.
    genesis(g)
    ev = log.record("p", OP_EDGE_ADD, {"edge": {"from": "seed:root", "to": "seed:root", "type": "SELF", "properties": {}}})
    apply_event(g, ev)

    report = replay(g, log, genesis=genesis)
    assert report["genesis_nodes"] == 1
    assert report["events_applied"] == 1
    assert g.node("seed:root") is not None
    assert g.out("seed:root", "SELF")
    g.close()


def test_replay_time_travel_excludes_later_events(tmp_path):
    g, log = _fresh(tmp_path)
    ev1 = log.record("p", OP_NODE_UPSERT, _node_payload("concept:early", labels=["Concept"]))
    apply_event(g, ev1)
    cutoff = ev1.ts
    time.sleep(0.01)
    ev2 = log.record("p", OP_NODE_UPSERT, _node_payload("concept:late", labels=["Concept"]))
    apply_event(g, ev2)

    replay(g, log, upto_ts=cutoff)
    assert g.node("concept:early") is not None
    assert g.node("concept:late") is None
    g.close()


def test_batch_op_is_replayable(tmp_path):
    """A BATCH event adds many nodes + edges and survives a replay."""
    g, log = _fresh(tmp_path)
    payload = {
        "nodes": [
            {"id": "n:1", "kind": "N", "name": "1", "labels": [], "properties": {}},
            {"id": "n:2", "kind": "N", "name": "2", "labels": [], "properties": {}},
        ],
        "edges": [{"from": "n:1", "to": "n:2", "type": "LINK", "properties": {}}],
    }
    ev = log.record("batcher", OP_BATCH, payload)
    apply_event(g, ev)
    assert g.node("n:1") is not None and g.node("n:2") is not None
    assert g.out("n:1", "LINK")

    replay(g, log)
    assert g.node("n:1") is not None
    assert g.out("n:1", "LINK")
    g.close()
