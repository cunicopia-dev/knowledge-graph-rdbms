# SQLite vs Postgres

The same operations, the same methodology (p50–p99 distributions, the shared
`bench/benchmark.py` harness), run against both live engines through the one
`GraphBackend` interface — so the only variable is the engine.

```bash
# bring up a throwaway Postgres (skips cleanly if none is reachable)
docker run -d --name kgrdbms-pg -e POSTGRES_USER=kg -e POSTGRES_PASSWORD=kg \
    -e POSTGRES_DB=kg -p 55432:5432 postgres:16-alpine

pip install "knowledge-graph-rdbms[postgres]"
python bench/postgres/benchmark.py                     # defaults
python bench/postgres/benchmark.py --scale 20000 --dsn postgresql://…
python bench/postgres/benchmark.py --json > pg.json
```

## What it's actually testing

Not "which engine is faster." **Which *shape* of work each engine is for.** The
only thing that changes between the two columns is where the data lives — in
process (SQLite) or across a TCP connection (Postgres) — so every number is a
clean read on the cost of that one difference.

## The result (illustrative — Apple Silicon, CPython 3.14, PG 16)

```
operation                                             sqlite    postgres  pg/sqlite
node(id) point lookup                                   6.62      392.42      59.2x
out(id) 1-hop                                          10.17      489.10      48.1x
neighborhood(depth=2)  [BFS: per-hop queries]         204.62    6,189.17      30.2x
shortest_path (chain ≤200)  [BFS: per-hop queries]    1,082.31  72,989.06      67.4x
descendants (chain 200)  [recursive CTE: 1 query]     2,617.29   1,377.31       0.5x  ← pg wins
nodes_by_kind (hydrate N)                              17.00       31.69       1.9x
add_nodes() bulk                                       21.25      256.92      12.1x
add_edges() bulk                                       39.94      244.62       6.1x
service.upsert_node (gated+logged, batched)            75.74    5,113.90      67.5x
```
(latency rows in µs/call, throughput rows in ms/batch; lower is better.)

## What to take from it

**Embedded SQLite owns the small, frequent ops.** A point lookup is an
in-process B-tree probe (~7µs); the same call to Postgres pays a localhost
round-trip (~0.4ms) before it touches data. For the agent-memory bread and
butter — read-heavy, shallow, lots of tiny queries — embedded wins by 30–60×,
the same shape as the [Neo4j comparison](../neo4j/README.md).

**The round-trip count is the whole story, and one row proves it.**
`descendants` and `shortest_path` walk the *same* 200-deep chain. `descendants`
is a recursive CTE — **one** query, evaluated server-side — and Postgres
actually *wins* it (0.5×): its planner is good and the round-trip is amortized
over the whole walk. `shortest_path` is a Python BFS that issues **one query per
hop** — 200 round-trips — and Postgres is 67× *slower*. Identical traversal,
opposite verdict, decided entirely by how many times the work crosses the wire.

**This benchmark specifies the next optimization.** The fix for the per-hop BFS
on a networked backend is to push it server-side: override `shortest_path` /
`neighborhood` with recursive CTEs for non-embedded engines (the way
`descendants` already is), instead of inheriting the in-process BFS that's free
on SQLite and pathological over TCP. The numbers above are the argument for it.

**So when is Postgres the right call?** Not for raw single-thread latency — for
*concurrency and scale*: many simultaneous writers, a dataset past one machine's
SQLite comfort zone, or a graph you want to share with other Postgres tooling.
The control plane lets you make that choice **per ontology**: keep the small,
hot ones embedded and escalate only the one that's earned it.
