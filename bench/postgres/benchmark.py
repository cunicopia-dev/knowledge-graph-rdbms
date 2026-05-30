#!/usr/bin/env python3
"""SQLite vs Postgres — same operations, same methodology, side by side.

Reuses the main `bench/benchmark.py` harness (p50–p99 distributions, the same
`bench_latency`/`bench_throughput` helpers) and runs an identical op suite
against both engines through the `GraphBackend` surface, so the only variable is
the engine.

    python bench/postgres/benchmark.py                      # sensible defaults
    python bench/postgres/benchmark.py --scale 20000 --dsn postgresql://…
    python bench/postgres/benchmark.py --json > pg.json

Bring up a throwaway Postgres first (skips cleanly if none is reachable):

    docker run -d --name kgrdbms-pg -e POSTGRES_USER=kg -e POSTGRES_PASSWORD=kg \
        -e POSTGRES_DB=kg -p 55432:5432 postgres:16-alpine

What you are meant to SEE, not just measure:

  * Embedded SQLite wins the small, frequent ops — a point lookup is an
    in-process B-tree probe; the same call to Postgres pays a TCP round-trip
    before it touches data. This is the agent-memory bread and butter.
  * `descendants` (a recursive CTE) is ONE round-trip either way, so Postgres
    stays competitive on deep server-side traversal.
  * `shortest_path` / `neighborhood` are Python BFS that issue one query PER HOP.
    In-process that's free; over a network it pays the round-trip N times — the
    pathological case, and a signpost for a real optimization (push BFS into a
    CTE for non-embedded backends).

So this isn't "which engine is faster" — it's "which SHAPE of work each engine
is for," measured honestly.
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import sys
import tempfile
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent.parent      # the bench/ dir
ROOT = BENCH_DIR.parent                                  # repo root
sys.path.insert(0, str(ROOT))                            # for `kgrdbms`
sys.path.insert(0, str(BENCH_DIR))                       # for `benchmark`

import benchmark as bm                                   # noqa: E402  the main harness
from kgrdbms import __version__, service                 # noqa: E402
from kgrdbms.events import EventLog                       # noqa: E402
from kgrdbms.graph import Graph                            # noqa: E402

DEFAULT_DSN = "postgresql://kg:kg@localhost:55432/kg"


# ---- engine fixtures -------------------------------------------------


def _populate(g, n, chain, rng):
    """Same working set in any backend: N nodes, ~2N random edges, one deep chain."""
    g.add_nodes(bm._node_specs(n))
    g.add_edges([(f"n:{rng.randrange(n)}", f"n:{rng.randrange(n)}", "REL") for _ in range(2 * n)])
    g.add_nodes([{"id": f"c:{i}", "kind": "Chain", "name": str(i)} for i in range(chain + 1)])
    g.add_edges([(f"c:{i}", f"c:{i + 1}", "NEXT") for i in range(chain)])
    return g


def _sqlite_backend():
    return Graph(path=Path(tempfile.mkdtemp()) / "bench.db")


def _postgres_backend(dsn):
    from kgrdbms.backends.postgres import PostgresGraph
    g = PostgresGraph(dsn)
    g.clear()
    return g


# ---- the shared op suite (run identically per engine) ----------------


def run_suite(g, *, engine, n, chain, iters, repeats, rng) -> list:
    """Run the comparison ops on backend `g`, tagging each Result with the engine."""
    out: list = []

    def add(r):
        r.name = f"[{engine}] {r.name}"
        out.append(r)
        print(f"  ✓ {r.name}", file=sys.stderr)

    # cheap latency ops — full iteration budget
    lookup_ids = [f"n:{rng.randrange(n)}" for _ in range(iters)]
    add(bm.bench_latency("node(id) point lookup", g.node, lookup_ids,
                         warmup=min(1000, iters // 10), unit="lookup"))

    out_ids = [f"n:{rng.randrange(n)}" for _ in range(iters)]
    add(bm.bench_latency("out(id) 1-hop", g.out, out_ids,
                         warmup=min(1000, iters // 10), unit="traversal"))

    # per-hop BFS ops — few samples (each is many round-trips on a networked engine)
    nb_ids = [f"n:{rng.randrange(n)}" for _ in range(min(iters, 200))]
    add(bm.bench_latency("neighborhood(depth=2)  [BFS: per-hop queries]",
                         lambda i: g.neighborhood(i, depth=2), nb_ids, warmup=10, unit="query"))

    sp_items = []
    for _ in range(min(iters, 40)):
        a = rng.randrange(chain)
        b = rng.randrange(a, chain + 1)
        sp_items.append((f"c:{a}", f"c:{b}"))
    add(bm.bench_latency(f"shortest_path (chain ≤{chain})  [BFS: per-hop queries]",
                         lambda it: g.shortest_path(it[0], it[1], max_depth=chain + 1),
                         sp_items, warmup=3, unit="path"))

    # recursive CTE — ONE round-trip even on Postgres
    de_items = [("c:0", "NEXT")] * min(iters, 60)
    add(bm.bench_latency(f"descendants (chain {chain})  [recursive CTE: 1 query]",
                         lambda it: g.descendants(it[0], it[1], max_depth=chain + 1),
                         de_items, warmup=3, unit="walk"))

    # bulk read
    add(bm.bench_throughput("nodes_by_kind (hydrate N)", prepare=lambda: g,
                            run=lambda gg: gg.nodes_by_kind("Node"), cleanup=lambda gg: None,
                            repeats=repeats, warmup=1, units=n, unit="node"))
    return out


def run_writes(make_backend, cleanup, *, engine, n, repeats, rng) -> list:
    """Write-throughput ops need a *fresh* backend per repeat, so they're separate."""
    out: list = []
    specs = bm._node_specs(n)
    edges = [(f"n:{rng.randrange(n)}", f"n:{rng.randrange(n)}", "REL") for _ in range(2 * n)]

    def add(r):
        r.name = f"[{engine}] {r.name}"
        out.append(r)
        print(f"  ✓ {r.name}", file=sys.stderr)

    add(bm.bench_throughput("add_nodes() bulk", make_backend,
                            lambda g: g.add_nodes(specs), cleanup,
                            repeats=repeats, warmup=1, units=n, unit="node"))

    def prep_edges():
        g = make_backend()
        g.add_nodes(specs)
        return g

    add(bm.bench_throughput("add_edges() bulk", prep_edges,
                            lambda g: g.add_edges(edges), cleanup,
                            repeats=repeats, warmup=1, units=len(edges), unit="edge"))

    # gated + logged path (CLI/MCP path). For postgres this also exercises the
    # control-plane SQLite event-log sidecar on every write.
    def prep_logged():
        g = make_backend()
        return (g, EventLog(g) if engine == "sqlite" else _logged_pg(g))

    def run_logged(ctx):
        g, ev = ctx
        with g.batch():
            for s in specs:
                service.upsert_node(g, ev, id=s["id"], kind=s["kind"], name=s["name"],
                                    labels=s["labels"], properties=s["properties"], actor="bench")

    add(bm.bench_throughput("service.upsert_node (gated+logged, batched)",
                            prep_logged, run_logged, lambda ctx: cleanup(ctx[0]),
                            repeats=repeats, warmup=1, units=n, unit="node"))
    return out


def _logged_pg(g):
    """A control-plane SQLite log paired with a postgres projection (mirrors resolver)."""
    import sqlite3
    from contextlib import contextmanager

    class _Store:
        def __init__(self):
            self.conn = sqlite3.connect(Path(tempfile.mkdtemp()) / "events.db")
            self.conn.row_factory = sqlite3.Row

        @contextmanager
        def tx(self):
            try:
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    return EventLog(_Store(), projection=g)


# ---- comparison report -----------------------------------------------


def print_comparison(env, sqlite_results, pg_results) -> None:
    print("kgrdbms — SQLite vs Postgres")
    print(f"  {env['implementation']} {env['python']} · SQLite {env['sqlite']} · {env['postgres']}")
    print(f"  {env['platform']}")
    p = env["params"]
    print(f"  scale={p['scale']:,} nodes · chain={p['chain']} · seed={p['seed']}")
    print()

    # pair results by the op name with the engine tag stripped
    def key(r):
        return r.name.split("] ", 1)[1]

    sq = {key(r): r for r in sqlite_results}
    pg = {key(r): r for r in pg_results}

    hdr = (f"  {'operation':<48}{'sqlite':>12}{'postgres':>12}{'pg/sqlite':>11}")
    print("Per-call latency (µs) / per-batch (ms) — lower is better; ratio >1 means Postgres slower")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for k in sq:
        if k not in pg:
            continue
        a, b = sq[k], pg[k]
        if a.category == "latency":
            av, bv = a.p50_us, b.p50_us
        else:
            av, bv = a.p50_us / 1000.0, b.p50_us / 1000.0  # ms
        ratio = (bv / av) if av > 0 else float("inf")
        flag = "  ← CTE: pg competitive" if "recursive CTE" in k else (
               "  ← per-hop round-trips" if "per-hop" in k else "")
        print(f"  {k:<48}{av:>12,.2f}{bv:>12,.2f}{ratio:>10.1f}x{flag}")
    print()
    print("  Read it as workload-shape, not a winner: embedded wins small frequent ops;")
    print("  the recursive-CTE traversal stays competitive; per-hop BFS is the network's tax.")


def environment(args, pg_version) -> dict:
    import sqlite3
    return {
        "kgrdbms": __version__,
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "sqlite": sqlite3.sqlite_version,
        "postgres": pg_version,
        "platform": platform.platform(),
        "params": {"scale": args.scale, "chain": args.chain, "iterations": args.iterations,
                   "repeats": args.repeats, "seed": args.seed},
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="SQLite vs Postgres benchmark for kgrdbms.")
    ap.add_argument("--dsn", default=__import__("os").environ.get("KGRDBMS_TEST_PG_DSN", DEFAULT_DSN))
    ap.add_argument("--scale", type=int, default=5_000, help="nodes in the working set (default 5000)")
    ap.add_argument("--chain", type=int, default=200, help="depth of the traversal chain (default 200)")
    ap.add_argument("--iterations", type=int, default=5_000, help="latency samples for cheap ops")
    ap.add_argument("--repeats", type=int, default=5, help="repeats per bulk op")
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:
        import psycopg
    except ImportError:
        print("postgres benchmark needs the extra: pip install 'knowledge-graph-rdbms[postgres]'",
              file=sys.stderr)
        return 0
    try:
        with psycopg.connect(args.dsn, connect_timeout=3) as c:
            pg_version = c.execute("select version()").fetchone()[0].split(",")[0]
    except Exception as e:
        print(f"no Postgres reachable at {args.dsn} ({e}); skipping.", file=sys.stderr)
        print("bring one up: docker run -d --name kgrdbms-pg -e POSTGRES_USER=kg "
              "-e POSTGRES_PASSWORD=kg -e POSTGRES_DB=kg -p 55432:5432 postgres:16-alpine",
              file=sys.stderr)
        return 0

    print("running SQLite vs Postgres benchmark…", file=sys.stderr)
    results = []
    for engine in ("sqlite", "postgres"):
        rng = random.Random(args.seed)  # identical workload per engine
        make = _sqlite_backend if engine == "sqlite" else (lambda: _postgres_backend(args.dsn))
        cleanup = bm._close_rm if engine == "sqlite" else (lambda g: g.close())
        g = make()
        _populate(g, args.scale, args.chain, rng)
        results += run_suite(g, engine=engine, n=args.scale, chain=args.chain,
                             iters=args.iterations, repeats=args.repeats, rng=rng)
        cleanup(g)
        results += run_writes(make, cleanup, engine=engine, n=args.scale,
                              repeats=args.repeats, rng=random.Random(args.seed))

    env = environment(args, pg_version)
    sqlite_results = [r for r in results if r.name.startswith("[sqlite]")]
    pg_results = [r for r in results if r.name.startswith("[postgres]")]

    if args.json:
        from dataclasses import asdict
        print(json.dumps({"environment": env, "results": [asdict(r) for r in results]}, indent=2))
    else:
        print()
        print_comparison(env, sqlite_results, pg_results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
