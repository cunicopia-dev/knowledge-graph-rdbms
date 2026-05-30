#!/usr/bin/env python3
"""Head-to-head: kgrdbms (embedded) vs Neo4j (server), identical graph.

An honest comparison of an in-process SQLite label property graph against a real
graph database. The point is to find the crossover: where the embedded model
wins outright (small, frequent ops — no network round-trip) and where a
purpose-built traversal engine starts to earn its keep (deep variable-length
walks).

Both sides load the SAME generated graph (seeded RNG) and run the SAME logical
queries, each measured the same way: warmup, then per-call latency over many
samples, reported as p50/p90/p99. kgrdbms runs in-process; Neo4j runs over the
Bolt protocol to a server — that round-trip is inherent to the server model, and
measuring it IS the experiment.

Prereqs:
  * a running Neo4j  (see bench/neo4j/README.md — one `docker run`)
  * pip install neo4j

    python bench/neo4j/headtohead.py
    python bench/neo4j/headtohead.py --scale 50000 --json > temp/neo4j.json
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # repo root for kgrdbms
from kgrdbms.graph import Graph  # noqa: E402

try:
    from neo4j import GraphDatabase
except ImportError:
    sys.exit("needs the neo4j driver:  pip install neo4j")


# ---- stats -----------------------------------------------------------


def percentile(ordered, p):
    n = len(ordered)
    if n == 0:
        return 0.0
    if n == 1:
        return ordered[0]
    k = (n - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, n - 1)
    return ordered[lo] * (1 - (k - lo)) + ordered[hi] * (k - lo)


def bench(label, fn, items, *, warmup):
    for it in items[:warmup]:
        fn(it)
    perf = time.perf_counter_ns
    durs = [0] * len(items)
    for i, it in enumerate(items):
        t = perf()
        fn(it)
        durs[i] = perf() - t
    durs.sort()
    us = 1000.0
    return {
        "label": label,
        "samples": len(durs),
        "p50_us": percentile(durs, 50) / us,
        "p90_us": percentile(durs, 90) / us,
        "p99_us": percentile(durs, 99) / us,
        "mean_us": statistics.fmean(durs) / us,
    }


def chunks(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ---- data (identical for both engines) ------------------------------


def make_data(n, chain_len, rng):
    nodes = [{"id": f"n:{i}", "v": f"v{i}"} for i in range(n)]
    rels = [{"f": f"n:{rng.randrange(n)}", "t": f"n:{rng.randrange(n)}"} for _ in range(2 * n)]
    chain = [f"c:{i}" for i in range(chain_len + 1)]
    chain_edges = [{"f": f"c:{i}", "t": f"c:{i+1}"} for i in range(chain_len)]
    return nodes, rels, chain, chain_edges


# ---- kgrdbms side ----------------------------------------------------


def load_kgrdbms(nodes, rels, chain, chain_edges):
    g = Graph(path=Path(tempfile.mkdtemp()) / "h2h.db")
    g.add_nodes([{"id": r["id"], "kind": "N", "name": r["id"], "properties": {"v": r["v"]}} for r in nodes])
    g.add_edges([(r["f"], r["t"], "REL") for r in rels])
    g.add_nodes([{"id": c, "kind": "C"} for c in chain])
    g.add_edges([(e["f"], e["t"], "NEXT") for e in chain_edges])
    return g


# ---- neo4j side ------------------------------------------------------


def load_neo4j(driver, nodes, rels, chain, chain_edges):
    with driver.session() as s:
        s.run("CREATE CONSTRAINT n_id IF NOT EXISTS FOR (n:N) REQUIRE n.id IS UNIQUE")
        s.run("CREATE CONSTRAINT c_id IF NOT EXISTS FOR (c:C) REQUIRE c.id IS UNIQUE")
        s.run("MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS")
        for b in chunks(nodes, 5000):
            s.run("UNWIND $rows AS r CREATE (n:N {id: r.id, v: r.v})", rows=b)
        for b in chunks([{"id": c} for c in chain], 5000):
            s.run("UNWIND $rows AS r CREATE (c:C {id: r.id})", rows=b)
        for b in chunks(rels, 5000):
            s.run("UNWIND $rows AS r MATCH (a:N {id: r.f}), (b:N {id: r.t}) CREATE (a)-[:REL]->(b)", rows=b)
        for b in chunks(chain_edges, 5000):
            s.run("UNWIND $rows AS r MATCH (a:C {id: r.f}), (b:C {id: r.t}) CREATE (a)-[:NEXT]->(b)", rows=b)


# ---- comparison ------------------------------------------------------


def run(args):
    rng = random.Random(args.seed)
    N = args.scale
    CHAIN = min(1000, max(50, N // 10))
    nodes, rels, chain, chain_edges = make_data(N, CHAIN, rng)

    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = os.environ.get("NEO4J_USER", "neo4j")
    pw = os.environ.get("NEO4J_PASSWORD", "benchpass")
    driver = GraphDatabase.driver(uri, auth=(user, pw))
    driver.verify_connectivity()

    print("  loading kgrdbms (in-process)…", file=sys.stderr)
    g = load_kgrdbms(nodes, rels, chain, chain_edges)
    print("  loading neo4j (server)…", file=sys.stderr)
    load_neo4j(driver, nodes, rels, chain, chain_edges)

    cheap = min(args.iterations, 3000)
    exp = min(args.iterations, 150)
    lookup_ids = [f"n:{rng.randrange(N)}" for _ in range(cheap)]
    hop_ids = [f"n:{rng.randrange(N)}" for _ in range(cheap)]
    sp_pairs = []
    for _ in range(exp):
        a = rng.randrange(CHAIN)
        b = rng.randrange(a, CHAIN + 1)
        sp_pairs.append((f"c:{a}", f"c:{b}"))
    desc_items = [("c:0",)] * exp

    sess = driver.session()

    def neo_lookup(i):
        sess.run("MATCH (n:N {id: $id}) RETURN n.v AS v", id=i).single()

    def neo_hop(i):
        list(sess.run("MATCH (:N {id: $id})-[:REL]->(m) RETURN m.id AS id", id=i))

    def neo_path(it):
        sess.run("MATCH (a:C {id: $a}), (b:C {id: $b}) "
                 "MATCH p = shortestPath((a)-[:NEXT*1..1200]-(b)) RETURN length(p) AS L",
                 a=it[0], b=it[1]).single()

    def neo_desc(it):
        sess.run("MATCH (:C {id: $a})-[:NEXT*1..1000]->(m) RETURN count(m) AS c", a=it[0]).single()

    cases = [
        ("point lookup", lambda i: g.node(i), neo_lookup, lookup_ids, 200),
        ("1-hop traversal", lambda i: g.out(i, "REL"), neo_hop, hop_ids, 200),
        (f"shortest_path (≤{CHAIN})",
         lambda it: g.shortest_path(it[0], it[1], max_depth=CHAIN + 1), neo_path, sp_pairs, 5),
        (f"descendants ({CHAIN} deep)",
         lambda it: g.descendants(it[0], "NEXT", max_depth=CHAIN + 1), neo_desc, desc_items, 5),
    ]

    results = []
    for name, kg_fn, neo_fn, items, warm in cases:
        print(f"  benchmarking: {name}…", file=sys.stderr)
        kg = bench("kgrdbms", kg_fn, items, warmup=min(warm, len(items)))
        neo = bench("neo4j", neo_fn, items, warmup=min(warm, len(items)))
        results.append({"op": name, "kgrdbms": kg, "neo4j": neo,
                        "ratio_p50": (neo["p50_us"] / kg["p50_us"]) if kg["p50_us"] else None})

    sess.close()
    driver.close()
    g.close()
    return {"params": {"scale": N, "chain": CHAIN, "seed": args.seed},
            "neo4j_uri": uri, "results": results}


def fmt_us(us):
    return f"{us/1000:.2f}ms" if us >= 1000 else f"{us:.1f}µs"


def print_table(report):
    p = report["params"]
    print(f"\nkgrdbms (embedded)  vs  Neo4j (server)   —   {p['scale']:,} nodes, chain {p['chain']}\n")
    hdr = f"  {'operation':<26}{'kgrdbms p50':>14}{'neo4j p50':>14}{'kgrdbms p99':>14}{'neo4j p99':>14}{'kg speedup':>13}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for r in report["results"]:
        kg, neo, ratio = r["kgrdbms"], r["neo4j"], r["ratio_p50"]
        tag = f"{ratio:.0f}× faster" if ratio and ratio >= 1 else (f"{1/ratio:.1f}× slower" if ratio else "—")
        print(f"  {r['op']:<26}{fmt_us(kg['p50_us']):>14}{fmt_us(neo['p50_us']):>14}"
              f"{fmt_us(kg['p99_us']):>14}{fmt_us(neo['p99_us']):>14}{tag:>13}")
    print("\n  p50 = median per-call latency. kgrdbms is in-process; neo4j is over Bolt (localhost).")
    print("  'speedup' = neo4j_p50 / kgrdbms_p50 (how many× the embedded path wins, or loses, per call).")


def main(argv=None):
    ap = argparse.ArgumentParser(description="kgrdbms vs Neo4j head-to-head.")
    ap.add_argument("--scale", type=int, default=20_000)
    ap.add_argument("--iterations", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    report = run(args)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_table(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
