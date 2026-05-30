# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A label property graph (nodes, typed directed edges, labels, JSON properties) stored in SQLite. The core library has **zero third-party dependencies** — everything is stdlib + SQLite. Three front doors (Python library, `kg` CLI, MCP server) sit on one gated + logged engine, and a **control plane** (`resolver.py` + the `backends/` package) lets all three address *many named ontologies* — each its own file (or, eventually, its own engine) — through one interface. See `README.md` for the full design narrative.

## Commands

```bash
uv venv && uv pip install -e ".[dev]"   # set up dev env (installs pytest + mcp)

pytest                                   # run all tests (~54)
pytest tests/test_graph.py               # one file
pytest tests/test_graph.py::test_name    # one test
pytest -k events                         # by keyword

python bench/benchmark.py                # perf, full p50–p99 distributions
python bench/charts.py                   # render assets/*.png from bench data (needs [charts])
python bench/runtimes/compare.py         # CPython vs Node vs Bun SQLite comparison

kg stats                                 # default ontology (~/.kgrdbms/graph.db)
kg ontology list                         # the registry (the "db of dbs")
kg ontology create coffee --stance inferential   # register a named ontology
kg --ontology coffee node add drink:latte --kind Drink   # route to it (resolver)
kg --db /tmp/x.db node add a:1 --kind T  # raw escape hatch: exact file, no registry
kg serve                                 # run the MCP server (needs [mcp] extra)
```

There is no linter/formatter configured — match the surrounding style (type hints, `from __future__ import annotations`, dataclasses).

## Architecture: the load-bearing ideas

**Two write paths, and the distinction is the whole point.**

- **Direct path** — `graph.py` methods (`g.add_node`, `g.add_nodes`, `g.batch()`). Writes go straight to the SQLite projection. Fast, but **not gated and not logged** — `replay()` will not reproduce them. Use for bulk loading raw data.
- **Logged path** — everything in `service.py` (used by the CLI and MCP server). Each mutation is gated, then applied, then appended to the `graph_events` log. Audited, reversible, replayable.

When you add or change a mutation, decide which path it belongs to. A new gated operation must go through `service.py`, not directly on `Graph`.

**The graph is a projection; the event log is the source of truth.** `events.py` holds an append-only log. `replay(graph, events, genesis=...)` rebuilds the projection from an optional declarative seed + the log, optionally `upto_ts=` for time travel. Undo is `compensate()` — it appends the *inverse* event, never deletes a row. If you add a new logged operation, you must also make it replayable and compensatable in `events.py` (add an `OP_*` constant + apply/compensate handling).

**The two-layer mutation gate, in order.** `service.guard()` runs `invariants.enforce()` **before** `policy.mutation_check()`:

- `invariants.py` = mechanism. Compiled-in, no off switch, changing one is a code change. Default: no-op.
- `policy.py` = configuration. A single `mutation_check(ctx) -> Decision`. Default: permissive (allow all). The file has commented example policies at the bottom.

Order matters: a permissive or compromised policy can never re-open something an invariant sealed. Failures raise `InvariantViolation` (invariant) vs `PermissionError` (policy) — keep these distinct.

**Hooks resolve through their modules at call time.** `guard()` calls `invariants.enforce` / `policy.mutation_check` via the module, not a captured reference — so editing policy (or monkeypatching it in a test) takes effect across all three front doors at once. Don't `from policy import mutation_check` into the service and call it directly; that would break this.

## Control plane: ontologies, the resolver, the backend registry

The engine above (`graph.py` + `service.py` + the log + the gate) operates on **one** graph. The control plane lets the three front doors address **many named ontologies** through that same engine, without any of them knowing where an ontology physically lives.

**`resolver.py` maps an ontology *name* → a `Resolved(backend, events, entry)` bundle.** Callers say `resolve("coffee")`; the resolver looks the name up in an **index** (itself a kg — `<root>/index.db`, nodes of kind `Ontology`), opens the right backend, pairs it with an event log, and hands back the bundle. The front doors then call the *same* `service.*` functions as before — the gate and the log didn't move, only *which* `(graph, events)` pair gets passed in. That's why multi-tenancy was a small change: the single write path was already the choke point.

- **The default ontology is the legacy file.** `resolver._default_db_path` special-cases the default name → `<root>/graph.db`, so omitting `--ontology` / the `ontology` arg behaves exactly as the pre-resolver code did. Backward-compat lives in that one function; both front doors inherit it. Don't scatter default-path logic elsewhere.
- **Isolation is filesystem-shaped, not code-shaped.** Each named ontology is its own SQLite file under `<root>/ontologies/<slug>/graph.db`, with its own event log. No tenant-id columns, no row filtering — "coffee doesn't know Ada" because they're different files.

**`backends/` is the pluggable data plane (engine registry).** An engine is a factory `(*, location, **opts) -> GraphBackend` registered with `@backend("name")`. Adding one = write a module + decorate its factory + add one import line in `backends/__init__.py`. **No switch to edit** — `resolver._open_backend` does `get_backend(entry.backend)(location=...)` and never knows which engines exist.

- `backends/base.py` — the `GraphBackend` Protocol (the finite method surface `service.py` + reads depend on; `Graph` satisfies it *structurally*, zero changes to `graph.py`) and `_StubBackend` (raising skeleton so a half-built engine is still a routable, fail-loud `GraphBackend`).
- `sqlite.py` and `postgres.py` are **live**; `neo4j.py` is a **stub** (routes and fails per-call with what's missing; its docstring is the ADR for how it'd be built). `postgres.py` is the scale-up: same five-table model + query shapes ported to psycopg (`%s`, `ON CONFLICT`, `jsonb` properties, `= ANY(%s)`, the recursive-CTE `descendants`, BFS traversals reused verbatim). It needs the `postgres` extra (`psycopg`), imported lazily so `import kgrdbms.backends` works without it; a missing driver raises `NotImplementedError`. `location` for postgres is a DSN, not a file path — `register()` requires it for any non-sqlite backend.
- **The event log is decoupled from the backend.** `EventLog(store, projection=None)`: *store* is the SQLite that holds the log rows; *projection* is the `GraphBackend` that `compensate()`/replay apply to. They coincide for sqlite (`EventLog(graph)` — projection defaults to store, unchanged). For postgres the store is `resolver._ControlPlaneLogStore` (a `<root>/ontologies/<slug>/events.db` sidecar) and the projection is the `PostgresGraph` — so audit/replay/undo keep working with graph data in Postgres and history in SQLite. `apply_event` only calls `GraphBackend` methods, so it drives any backend. This store↔projection split is the seam to respect for *any* non-sqlite engine (neo4j next).
- **New failure class:** routing to a stub engine raises `NotImplementedError`. Every front door's error handling must account for it (the CLI's `main()` already maps it to `unavailable: …` / exit 1).

## Layout

```
kgrdbms/
├── graph.py        # the LPG over SQLite — imports nothing internal; usable standalone
├── events.py       # append-only event log: OP_* constants, record, compensate, replay
├── policy.py       # configurable mutation policy (edit mutation_check)
├── invariants.py   # compiled-in invariants, run before policy (no-op default)
├── service.py      # the shared gated + logged write path (all front doors use this)
├── resolver.py     # control plane: name → (backend, events, entry); the ontology index
├── backends/       # pluggable data plane (engine registry)
│   ├── base.py     #   GraphBackend Protocol + _StubBackend
│   ├── __init__.py #   registry: @backend(name), get_backend, available_backends
│   ├── sqlite.py   #   live engine (adapter over Graph)
│   ├── postgres.py #   live engine (psycopg; jsonb + recursive CTEs); needs [postgres] extra
│   └── neo4j.py    #   stub (deep-traversal escalation)
├── cli.py          # the `kg` command (stdlib argparse) — `--ontology` / `--db` / `kg ontology …`
└── mcp_server.py   # MCP server, kg_-prefixed tools, each with optional `ontology=` (optional [mcp] extra)
```

`graph.py` has no internal dependencies — everything else layers on top of it. Dependency direction: `graph` ← `events`/`backends` ← `resolver` ← `service`-callers (`cli`, `mcp_server`). `service.py` depends only on the `GraphBackend` surface, never a concrete engine. Public API is re-exported from `__init__.py`.

## Node id convention (CURIEs)

Node ids follow `prefix:reference` — `person:ada-lovelace`, `company:apple`, `card:abc123`. This is deliberately a **CURIE** (a compact URI: the prefix is shorthand that expands to a full IRI through a lookup table you only need the day you publish to the RDF/linked-data world). Adopting the shape now keeps interop a cheap, additive option later; until then a CURIE stands alone as a plain string. Three rules:

1. **`prefix`** is a short, stable, lowercase type word (`person`, `company`, `card`, `device`) — not the `kind` field's exact casing, just a stable token.
2. **`reference`** is slugged. Mint ids with `slug(name, prefix="person")` → `person:ada-lovelace`. **`slug()` is the CURIE constructor and the dedup mechanism** — but `add_node` does *not* auto-slug the id it's handed, so an id minted by hand (`"person:Ada"`) will *not* collapse with `slug()`-minted ones. Always go through `slug()` when the local part comes from natural language.
3. **The id is an address, not a record.** Put identity in the id (`company/apple`), never mutable attributes (`status=active`) — those are node *properties*. Mental model: the id is a URL's *path* (stable), properties are its *query string* (changeable). Baking a changeable fact into an id breaks identity when the fact changes.

**Namespacing is free and you don't type it.** Each ontology is its own file with its own registry name, so "which world a node came from" is already known — the ontology *is* the namespace. If two ontologies are ever merged, qualify by ontology name; you don't pre-encode it in the id. An ontology that genuinely needs strict global identity sets its `id_convention` on the registry entry (currently descriptive metadata; the per-ontology enforcement seam, not yet wired to a validator) and adopts fuller CURIE/IRI discipline without affecting the others.

**No RDF stack.** This is the *only* RDF idea adopted (identity, because it's the one expensive-to-retrofit decision). No OWL, no SPARQL, no triplestore-as-storage. Interop (Turtle/JSON-LD) and vocabulary borrowing (SKOS, PROV-O) are deferred until a real external consumer exists — both are additive at the boundary, store LPG inside.

## Conventions that bite

- **Edges are unique on `(from_node, type, to_node)`.** Re-adding the same triple updates properties rather than duplicating — mutations are idempotent by construction. Tests rely on this.
- **`slug()` deduplicates natural-language ids** — two strings that slugify the same collapse to one node id (see *Node id convention* above; `add_node` does not auto-slug).
- **Properties round-trip as JSON.** Storage is `value_json`; ints/bools/lists/objects come back as their JSON type. CLI `--prop key=value` parses value as JSON when possible, else keeps it as a string.
- **Per-call writes each commit (one fsync).** Wrapping work in `batch()` / using `add_nodes` / `add_edges` collapses to one transaction (~10× faster). Don't add a per-row commit inside a bulk loop.
- **Reads are not in `service.py`** by design — callers hit `Graph` directly. Don't route reads through the gate.
- **CLI exit codes are contractual:** `0` ok, `1` not found / bad input, `2` policy denial, `3` invariant violation. Preserve these.
