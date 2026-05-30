# Benchmarks

Two things live here:

1. **`benchmark.py`** — the canonical benchmark of kgrdbms itself. Standard
   library only, so anyone with Python can run it on their own machine.
2. **`runtimes/`** — an optional appendix that runs the *same raw SQLite*
   workload across CPython, Node, and Bun, to show how much the language
   runtime's binding actually costs (spoiler: less than you'd think).

All numbers are distributions — mean and p50/p90/p95/p99 — because a single
timing is noise, not data.

---

## 1. The kgrdbms benchmark

```bash
python bench/benchmark.py                                  # defaults
python bench/benchmark.py --scale 50000 --iterations 100000 --repeats 15
python bench/benchmark.py --json > results.json            # machine-readable
```

**Methodology.** Two measurement modes, kept deliberately separate because they
answer different questions:

- **Per-operation latency** — cheap ops (a point lookup) are timed one call at a
  time over `--iterations` samples. The percentiles describe *per-call* cost in
  microseconds. This is the right lens for "how fast is one lookup, and how bad
  is the tail?"
- **Bulk throughput** — a whole batch (insert N nodes) is timed as a single
  sample and repeated `--repeats` times. The percentiles describe *per-batch*
  time in milliseconds; the derived ops/sec is the sustained rate. This is the
  right lens for "how fast can I load data?"

Every op gets warmup iterations first (JIT/cache priming, page-cache fill), and
the RNG is seeded (`--seed`) so runs are reproducible. To add your own workload,
append a case in `run()` — `bench_latency` and `bench_throughput` are the only
two helpers you need.

**Representative output** (Apple Silicon, CPython 3.14, SQLite 3.50 — yours will
differ; run it):

```
Per-operation latency  (microseconds per call; lower is better)
  operation                       mean     p50     p90     p95     p99   ops/sec
  node(id) point lookup           7.43    7.00    8.25    9.21   12.63   134,514
  out(id) 1-hop traversal        11.26   11.38   17.92   20.21   25.67    88,812
  neighborhood(depth=2)         252.42  240.77  404.38  464.64  588.67     3,962
  shortest_path (chain ≤1000)  6971    6284   14797   15591   16935       143

Bulk throughput  (ms per batch; ops/sec is sustained rate)
  operation                                 mean    p50    p90      ops/sec
  add_node (per-call commit)              612.04 610.89 620.70       16,339
  add_node inside batch()                  58.66  58.60  59.42      170,475
  add_nodes() bulk                         49.44  49.49  50.35      202,258
  service.upsert_node (gated+logged)      179.19 175.10 187.56       55,806
  replay() full log                       375.11 375.31 375.70       26,659
```

**What to read from it:**

- The **~10× write jump** from per-call `add_node` (16k/s) to `batch()` /
  `add_nodes` (170–202k/s) is the single most important number — it's the cost
  of one fsync per call versus one per batch.
- `service.upsert_node` (the gated + logged path the CLI and MCP server use)
  runs at ~56k/s — that's the price of the invariants+policy gate plus an event
  record per write, and it's still fast enough for interactive use.
- `shortest_path`'s p90 being ~2× its p50 is the workload talking: randomized
  endpoints mean some BFS walks are short and some span the whole chain. The
  tail is real; an "average" would hide it.

---

## 2. Cross-runtime SQLite comparison (optional appendix)

> Requires Node (`node --experimental-sqlite`) and/or Bun to be installed.
> Missing runtimes are skipped. This is **not** a kgrdbms benchmark — it's raw
> SQLite, to isolate binding/runtime FFI overhead.

```bash
python bench/runtimes/compare.py
BENCH_N=500000 BENCH_B=50000 BENCH_R=15 python bench/runtimes/compare.py
```

It runs three sibling probes over an identical workload and tabulates the
medians:

```
  runtime         SQLite       insert/call   insert/many        lookup
  CPython 3.14.2  3.50.4         1,772,755     2,819,234       768,884
  Node v22.15.0   3.49.1         2,608,276             —     1,014,638
  Bun 1.3.14      3.51.0         3,062,603             —       639,708
```

**The takeaway:** no runtime sweeps, and the whole spread is under ~2×. Bun has
the leanest insert path; Node the fastest single-row read; CPython's
`executemany` (which batches the bind loop in C — the JS bindings have no
equivalent) is competitive on bulk insert. It's the *same SQLite engine* under
all three, so what you're seeing is purely the cost of crossing from the
language into C and back.

**Caveats worth knowing before quoting these:**

- Each runtime bundles its **own SQLite version** (above: 3.49–3.51), so it's
  the same engine family, not byte-identical builds.
- `node:sqlite` is **experimental** and finalizes prepared statements out from
  under a tight loop at ~114k cumulative calls (`ERR_INVALID_STATE`). The probe
  keeps lookup blocks under that and notes if it trips.
- These are micro-throughput numbers on one machine. Run them on yours.

**Why this matters for kgrdbms:** the binding is a ≤2× lever and doesn't even
favor one runtime across operations. The lever that actually moved kgrdbms was
transaction batching (~10×, see above) — not the language. The interesting part
of the project (event sourcing, the gate, three front doors) is runtime-agnostic.
