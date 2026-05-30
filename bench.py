"""Ad-hoc performance probe for kgrdbms. Not a committed artifact."""
from __future__ import annotations

import os
import random
import tempfile
import time
from pathlib import Path

from kgrdbms.graph import Graph
from kgrdbms.events import EventLog, OP_NODE_UPSERT, apply_event, replay

random.seed(7)


class T:
    def __init__(self, label, n):
        self.label, self.n = label, n
    def __enter__(self):
        self.t = time.perf_counter(); return self
    def __exit__(self, *a):
        dt = time.perf_counter() - self.t
        rate = self.n / dt if dt else float("inf")
        print(f"{self.label:<42} {self.n:>8,} ops  {dt*1000:>9.1f} ms  {rate:>12,.0f}/s")


N_NODES = 20_000
N_EDGES = 40_000
N_LOOKUPS = 20_000

tmp = Path(tempfile.mkdtemp()) / "bench.db"
g = Graph(path=tmp)

def _node_specs(n):
    return [{"id": f"n:{i}", "kind": "K", "name": f"node {i}",
             "labels": ["Even"] if i % 2 == 0 else ["Odd"],
             "properties": {"i": i, "tag": f"t{i%100}"}} for i in range(n)]

# 1a. Node inserts via the public API (commit per call) — the baseline.
with T("add_node (commit/call)", N_NODES):
    for i in range(N_NODES):
        g.add_node(f"n:{i}", kind="K", name=f"node {i}",
                   labels={"Even"} if i % 2 == 0 else {"Odd"},
                   properties={"i": i, "tag": f"t{i%100}"})

# 1b. Same writes inside a batch() — one commit for the whole block.
gb = Graph(path=Path(tempfile.mkdtemp()) / "batch.db")
with T("add_node inside batch()", N_NODES):
    with gb.batch():
        for i in range(N_NODES):
            gb.add_node(f"n:{i}", kind="K", name=f"node {i}",
                        labels={"Even"} if i % 2 == 0 else {"Odd"},
                        properties={"i": i, "tag": f"t{i%100}"})
gb.close()

# 1c. Bulk add_nodes (executemany, single transaction) — the fast path.
gn = Graph(path=Path(tempfile.mkdtemp()) / "addnodes.db")
specs = _node_specs(N_NODES)
with T("add_nodes (bulk executemany)", N_NODES):
    gn.add_nodes(specs)
gn.close()

# 2a. Edge inserts via the public API (commit per call).
pairs = [(f"n:{random.randrange(N_NODES)}", f"n:{random.randrange(N_NODES)}", "REL")
         for _ in range(N_EDGES)]
with T("add_edge (commit/call)", N_EDGES):
    for a, b, typ in pairs:
        g.add_edge(a, b, typ)

# 2b. Bulk add_edges (executemany).
ge = Graph(path=Path(tempfile.mkdtemp()) / "addedges.db")
ge.add_nodes(_node_specs(N_NODES))
with T("add_edges (bulk executemany)", N_EDGES):
    ge.add_edges(pairs)
ge.close()

edges_actual = g.total_edges()

# 3. Point lookups.
ids = [f"n:{random.randrange(N_NODES)}" for _ in range(N_LOOKUPS)]
with T("node(id) point lookup", N_LOOKUPS):
    for nid in ids:
        g.node(nid)

# 4. nodes_by_label (full-set scan).
with T("nodes_by_label('Even')", 1):
    even = g.nodes_by_label("Even")
assert len(even) == N_NODES // 2

# 5. nodes_by_kind.
with T("nodes_by_kind('K')", 1):
    allk = g.nodes_by_kind("K")

# 6. out() traversal over many nodes.
with T("out() over 5,000 nodes", 5000):
    for i in range(5000):
        g.out(f"n:{i}", "REL")

# 7. neighborhood depth 2 (BFS, Python-side).
with T("neighborhood(depth=2) x500", 500):
    for i in range(500):
        g.neighborhood(f"n:{i}", depth=2)

# 8. recursive descendants along a built chain.
for i in range(2000):
    g.add_edge(f"chain:{i}", f"chain:{i+1}", "NEXT") if False else None
# build a clean chain
for i in range(1001):
    g.add_node(f"chain:{i}", "C", f"c{i}")
for i in range(1000):
    g.add_edge(f"chain:{i}", f"chain:{i+1}", "NEXT")
with T("descendants() down 1,000-deep chain", 1):
    d = g.descendants("chain:0", "NEXT", max_depth=2000)
assert len(d) == 1000, len(d)

# 9. shortest_path across the chain (BFS).
with T("shortest_path across 1,000-hop chain", 1):
    p = g.shortest_path("chain:0", "chain:1000", max_depth=2000)
assert p and len(p) == 1001

# 10. Event log: record + replay.
log = EventLog(g)
NEV = 10_000
with T("EventLog.record (commit/call)", NEV):
    for i in range(NEV):
        log.record("bench", OP_NODE_UPSERT, {
            "after": {"id": f"ev:{i}", "kind": "E", "name": str(i), "labels": [], "properties": {}},
            "prior": None,
        })
with T("replay() full log", 1):
    rep = replay(g, log)

print("-" * 86)
print(f"nodes={g.total_nodes():,}  edges={edges_actual:,}  events={log.count():,}  "
      f"db={os.path.getsize(tmp)/1e6:.1f} MB  replay_applied={rep['events_applied']:,}")

# ---- contrast: bulk insert inside ONE transaction (bypassing per-call commit) ----
tmp2 = Path(tempfile.mkdtemp()) / "bulk.db"
g2 = Graph(path=tmp2)
import json, uuid
with T("RAW bulk insert, single transaction", N_NODES):
    with g2.tx() as c:
        c.executemany("INSERT INTO nodes(id,kind,name) VALUES (?,?,?)",
                      [(f"n:{i}", "K", f"node {i}") for i in range(N_NODES)])
print("(^ shows the ceiling once per-call commit is removed)")
g.close(); g2.close()
