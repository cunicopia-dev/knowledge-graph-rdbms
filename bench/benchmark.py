#!/usr/bin/env python3
"""Reproducible benchmark for kgrdbms.

Runs each operation many times and reports a *distribution* — mean, p50, p90,
p95, p99 — not a single lucky number. Standard library only, so anyone with
Python can run it against their own machine:

    python bench/benchmark.py                          # sensible defaults
    python bench/benchmark.py --scale 50000 --iterations 100000 --repeats 15
    python bench/benchmark.py --json > results.json    # machine-readable

Two kinds of measurement, deliberately kept separate:

  * Per-operation LATENCY — cheap ops (a point lookup) are timed individually
    over many samples, so the percentiles describe per-call cost in microseconds.
  * Bulk THROUGHPUT — a whole batch (insert N nodes) is timed as one sample and
    repeated; the percentiles describe per-batch time in milliseconds, and the
    derived ops/sec tells you sustained rate.

Mixing those two into one "ops/sec" headline is how benchmarks mislead, so we
don't. To add your own workload, append a case in `run()` — the two helpers
(`bench_latency`, `bench_throughput`) are the only machinery you need.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import sqlite3
import statistics
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

# Run from a source checkout without installing.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kgrdbms import __version__, service                       # noqa: E402
from kgrdbms.events import EventLog, OP_NODE_UPSERT, replay     # noqa: E402
from kgrdbms.graph import Graph                                 # noqa: E402


# ---- statistics ------------------------------------------------------


def percentile(ordered: list[float], p: float) -> float:
    """Linear-interpolated percentile of an already-sorted list."""
    n = len(ordered)
    if n == 0:
        return 0.0
    if n == 1:
        return ordered[0]
    k = (n - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, n - 1)
    frac = k - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


@dataclass
class Result:
    name: str
    category: str          # "latency" | "throughput"
    samples: int           # number of timed samples
    units_per_sample: int  # 1 for latency; N for a bulk batch
    unit: str              # what one unit is, e.g. "node", "edge", "lookup"
    mean_us: float
    p50_us: float
    p90_us: float
    p95_us: float
    p99_us: float
    min_us: float
    max_us: float
    stdev_us: float
    ops_per_s: float


def _summarize(name, category, durs_ns, units_per_sample, unit) -> Result:
    ordered = sorted(durs_ns)
    NS_PER_US = 1_000.0
    mean_ns = statistics.fmean(durs_ns)
    stdev_ns = statistics.pstdev(durs_ns) if len(durs_ns) > 1 else 0.0
    mean_s = mean_ns / 1e9
    ops_per_s = (units_per_sample / mean_s) if mean_s > 0 else float("inf")
    return Result(
        name=name,
        category=category,
        samples=len(durs_ns),
        units_per_sample=units_per_sample,
        unit=unit,
        mean_us=mean_ns / NS_PER_US,
        p50_us=percentile(ordered, 50) / NS_PER_US,
        p90_us=percentile(ordered, 90) / NS_PER_US,
        p95_us=percentile(ordered, 95) / NS_PER_US,
        p99_us=percentile(ordered, 99) / NS_PER_US,
        min_us=ordered[0] / NS_PER_US,
        max_us=ordered[-1] / NS_PER_US,
        stdev_us=stdev_ns / NS_PER_US,
        ops_per_s=ops_per_s,
    )


# ---- measurement helpers ---------------------------------------------


def bench_latency(name, op, items, *, warmup, unit="op") -> Result:
    """Time `op(item)` once per item; each call is one timed sample.

    `items` is a presized list, so the timed loop does no allocation of its
    own — what you measure is the operation, not the harness.
    """
    perf = time.perf_counter_ns
    for it in items[:warmup]:
        op(it)
    durs = [0] * len(items)
    for idx in range(len(items)):
        it = items[idx]
        t0 = perf()
        op(it)
        durs[idx] = perf() - t0
    return _summarize(name, "latency", durs, 1, unit)


def bench_throughput(name, prepare, run, cleanup, *, repeats, warmup, units, unit) -> Result:
    """Time the whole `run(ctx)` batch; repeat it `repeats` times.

    `prepare()` builds fresh state (untimed), `run(ctx)` is the measured batch,
    `cleanup(ctx)` tears it down. Each repeat is one sample, so percentiles are
    over runs and the derived ops/sec uses `units` (batch size).
    """
    perf = time.perf_counter_ns
    for _ in range(warmup):
        ctx = prepare()
        run(ctx)
        cleanup(ctx)
    durs = []
    for _ in range(repeats):
        ctx = prepare()
        t0 = perf()
        run(ctx)
        durs.append(perf() - t0)
        cleanup(ctx)
    return _summarize(name, "throughput", durs, units, unit)


# ---- fixtures --------------------------------------------------------


def _fresh_graph() -> Graph:
    return Graph(path=Path(tempfile.mkdtemp()) / "bench.db")


def _close_rm(g: Graph) -> None:
    p = str(g.path)
    g.close()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(p + suffix)
        except OSError:
            pass


def _node_specs(n):
    return [
        {
            "id": f"n:{i}",
            "kind": "Node",
            "name": f"node {i}",
            "labels": ["Even"] if i % 2 == 0 else ["Odd"],
            "properties": {"i": i, "tag": f"t{i % 100}"},
        }
        for i in range(n)
    ]


def build_read_graph(n, chain_len, rng) -> Graph:
    """A populated graph for the read/traversal benchmarks (built once)."""
    g = _fresh_graph()
    g.add_nodes(_node_specs(n))
    g.add_edges([(f"n:{rng.randrange(n)}", f"n:{rng.randrange(n)}", "REL") for _ in range(2 * n)])
    g.add_nodes([{"id": f"c:{i}", "kind": "Chain", "name": str(i)} for i in range(chain_len + 1)])
    g.add_edges([(f"c:{i}", f"c:{i + 1}", "NEXT") for i in range(chain_len)])
    return g


# ---- the benchmark suite ---------------------------------------------


def run(args) -> list[Result]:
    rng = random.Random(args.seed)
    N = args.scale
    ITERS = args.iterations
    REPEATS = args.repeats
    CHAIN = min(1000, max(50, N // 10))
    node_specs = _node_specs(N)
    edge_pairs = [(f"n:{rng.randrange(N)}", f"n:{rng.randrange(N)}", "REL") for _ in range(2 * N)]
    results: list[Result] = []

    def add(r):
        results.append(r)
        if not args.json:
            print(f"  ✓ {r.name}", file=sys.stderr)

    # ---------- reads / traversal (latency) ----------
    read_g = build_read_graph(N, CHAIN, rng)

    lookup_ids = [f"n:{rng.randrange(N)}" for _ in range(ITERS)]
    add(bench_latency("node(id) point lookup", read_g.node, lookup_ids,
                      warmup=min(2000, ITERS // 10), unit="lookup"))

    out_ids = [f"n:{rng.randrange(N)}" for _ in range(ITERS)]
    add(bench_latency("out(id) 1-hop traversal", read_g.out, out_ids,
                      warmup=min(2000, ITERS // 10), unit="traversal"))

    nb_ids = [f"n:{rng.randrange(N)}" for _ in range(min(ITERS, 3000))]
    add(bench_latency("neighborhood(depth=2)",
                      lambda i: read_g.neighborhood(i, depth=2), nb_ids,
                      warmup=50, unit="query"))

    sp_items = []
    for _ in range(min(ITERS, 400)):
        a = rng.randrange(CHAIN)
        b = rng.randrange(a, CHAIN + 1)
        sp_items.append((f"c:{a}", f"c:{b}"))
    add(bench_latency(f"shortest_path (chain ≤{CHAIN})",
                      lambda it: read_g.shortest_path(it[0], it[1], max_depth=CHAIN + 1),
                      sp_items, warmup=5, unit="path"))

    de_items = [("c:0", "NEXT")] * min(ITERS, 400)
    add(bench_latency(f"descendants (chain {CHAIN} deep)",
                      lambda it: read_g.descendants(it[0], it[1], max_depth=CHAIN + 1),
                      de_items, warmup=5, unit="walk"))

    # nodes_by_kind returns all N — a bulk read, so measure it as throughput.
    add(bench_throughput("nodes_by_kind (hydrate N)",
                         prepare=lambda: read_g,
                         run=lambda g: g.nodes_by_kind("Node"),
                         cleanup=lambda g: None,
                         repeats=REPEATS, warmup=2, units=N, unit="node"))

    _close_rm(read_g)

    # ---------- writes (throughput) ----------
    def run_add_node(g):
        for s in node_specs:
            g.add_node(s["id"], s["kind"], s["name"], labels=s["labels"], properties=s["properties"])

    def run_batch_add_node(g):
        with g.batch():
            run_add_node(g)

    add(bench_throughput("add_node (per-call commit)", _fresh_graph, run_add_node, _close_rm,
                         repeats=REPEATS, warmup=1, units=N, unit="node"))
    add(bench_throughput("add_node inside batch()", _fresh_graph, run_batch_add_node, _close_rm,
                         repeats=REPEATS, warmup=1, units=N, unit="node"))
    add(bench_throughput("add_nodes() bulk", _fresh_graph, lambda g: g.add_nodes(node_specs), _close_rm,
                         repeats=REPEATS, warmup=1, units=N, unit="node"))

    def prepare_edges():
        g = _fresh_graph()
        g.add_nodes(node_specs)
        return g

    add(bench_throughput("add_edges() bulk", prepare_edges, lambda g: g.add_edges(edge_pairs), _close_rm,
                         repeats=REPEATS, warmup=1, units=len(edge_pairs), unit="edge"))

    # The gated + logged write path (what the CLI and MCP server use), batched.
    def prepare_logged():
        g = _fresh_graph()
        return (g, EventLog(g))

    def run_logged(ctx):
        g, ev = ctx
        with g.batch():
            for s in node_specs:
                service.upsert_node(g, ev, id=s["id"], kind=s["kind"], name=s["name"],
                                    labels=s["labels"], properties=s["properties"], actor="bench")

    add(bench_throughput("service.upsert_node (gated+logged, batched)",
                         prepare_logged, run_logged, lambda ctx: _close_rm(ctx[0]),
                         repeats=REPEATS, warmup=1, units=N, unit="node"))

    # ---------- event log (throughput) ----------
    def _evt(i):
        return {"after": {"id": f"e:{i}", "kind": "E", "name": str(i), "labels": [], "properties": {}},
                "prior": None}

    def run_record_batch(ctx):
        g, ev = ctx
        with g.batch():
            for i in range(N):
                ev.record("bench", OP_NODE_UPSERT, _evt(i))

    add(bench_throughput("EventLog.record (batched)", prepare_logged, run_record_batch,
                         lambda ctx: _close_rm(ctx[0]), repeats=REPEATS, warmup=1, units=N, unit="event"))

    def prepare_replay():
        g = _fresh_graph()
        ev = EventLog(g)
        with g.batch():
            for i in range(N):
                ev.record("bench", OP_NODE_UPSERT, _evt(i))
        return (g, ev)

    add(bench_throughput("replay() full log", prepare_replay,
                         lambda ctx: replay(ctx[0], ctx[1]), lambda ctx: _close_rm(ctx[0]),
                         repeats=max(3, REPEATS // 2), warmup=1, units=N, unit="event"))

    return results


# ---- presentation ----------------------------------------------------


def environment(args) -> dict:
    return {
        "kgrdbms": __version__,
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "params": {"scale": args.scale, "iterations": args.iterations,
                   "repeats": args.repeats, "seed": args.seed},
    }


def _fmt(x, width=10):
    if x >= 1000:
        return f"{x:>{width},.0f}"
    return f"{x:>{width}.2f}"


def print_report(env, results) -> None:
    print("kgrdbms benchmark")
    print(f"  {env['implementation']} {env['python']} · SQLite {env['sqlite']}")
    print(f"  {env['platform']}")
    p = env["params"]
    print(f"  scale={p['scale']:,} nodes · iterations={p['iterations']:,} · "
          f"repeats={p['repeats']} · seed={p['seed']}")
    print()

    lat = [r for r in results if r.category == "latency"]
    thr = [r for r in results if r.category == "throughput"]

    if lat:
        print("Per-operation latency  (microseconds per call; lower is better)")
        hdr = f"  {'operation':<30}{'n':>9}{'mean':>10}{'p50':>10}{'p90':>10}{'p95':>10}{'p99':>10}{'ops/sec':>13}"
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for r in lat:
            print(f"  {r.name:<30}{r.samples:>9,}{_fmt(r.mean_us)}{_fmt(r.p50_us)}"
                  f"{_fmt(r.p90_us)}{_fmt(r.p95_us)}{_fmt(r.p99_us)}{r.ops_per_s:>13,.0f}")
        print()

    if thr:
        print("Bulk throughput  (milliseconds per batch; ops/sec is sustained rate)")
        hdr = f"  {'operation':<44}{'runs':>6}{'mean':>9}{'p50':>9}{'p90':>9}{'p95':>9}{'ops/sec':>13}"
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for r in thr:
            ms = lambda us: us / 1000.0
            print(f"  {r.name:<44}{r.samples:>6}{_fmt(ms(r.mean_us), 9)}{_fmt(ms(r.p50_us), 9)}"
                  f"{_fmt(ms(r.p90_us), 9)}{_fmt(ms(r.p95_us), 9)}{r.ops_per_s:>13,.0f}")
        print()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Reproducible kgrdbms benchmark.")
    ap.add_argument("--scale", type=int, default=10_000, help="nodes in the working set (default 10000)")
    ap.add_argument("--iterations", type=int, default=20_000, help="latency samples per cheap op (default 20000)")
    ap.add_argument("--repeats", type=int, default=7, help="repeats per bulk op (default 7)")
    ap.add_argument("--seed", type=int, default=1234, help="RNG seed for reproducibility")
    ap.add_argument("--json", action="store_true", help="emit JSON (env + results) instead of a table")
    args = ap.parse_args(argv)

    if not args.json:
        print("running benchmark (this takes a moment)…", file=sys.stderr)
    results = run(args)
    env = environment(args)

    if args.json:
        print(json.dumps({"environment": env, "results": [asdict(r) for r in results]}, indent=2))
    else:
        print()
        print_report(env, results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
