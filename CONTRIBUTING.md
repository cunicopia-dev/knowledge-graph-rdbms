# Contributing

Thanks for your interest. This is a small, opinionated project — the goal is a
knowledge graph you can hold in your head, so contributions that keep it legible
are worth more than ones that add surface area.

## Setup

```bash
git clone https://github.com/cunicopia-dev/knowledge-graph-rdbms
cd knowledge-graph-rdbms
uv venv && uv pip install -e ".[dev]"     # pytest + mcp
pytest                                     # should be all green
```

The Postgres backend and its tests are optional — they need the `postgres` extra
and a reachable Postgres (`pip install -e ".[dev,postgres]"`); the suite skips
those tests cleanly when no database is available.

## The rules that matter here

These aren't style nits — they're the load-bearing invariants the design depends
on. A change that breaks one of these will be asked to change, no matter how nice
it looks.

1. **The core stays zero-dependency.** `kgrdbms` (the library, CLI, engine) imports
   only the standard library + SQLite. Third-party deps live behind extras
   (`mcp`, `postgres`, `charts`) and are imported lazily. Don't add a hard
   dependency to the core.

2. **Mutations go through the gate.** Any new *logged* operation must go through
   `service.py` (which runs `invariants.enforce` then `policy.mutation_check`,
   then records the event) — not directly on a backend. The direct `Graph`
   methods are the unlogged fast path, on purpose; know which one you're adding to.

3. **A new logged op must be replayable and reversible.** If you add an operation
   to the event log, add its `OP_*` constant plus `apply_event` and `compensate`
   handling in `events.py`. "Every mutation can be replayed and undone" is a
   guarantee, not a nice-to-have.

4. **Backends register; they don't get switched on.** A new engine is a module in
   `backends/` with a factory decorated `@backend("name")` and one import line in
   `backends/__init__.py`. It implements the `GraphBackend` protocol. No `if
   engine == ...` ladders anywhere.

5. **Node ids are CURIEs** (`prefix:reference`, minted via `slug()`). See the
   convention in `CLAUDE.md` / the README before inventing id shapes.

6. **CLI exit codes are contractual:** `0` ok · `1` not found / bad input · `2`
   policy denial · `3` invariant violation · (`unavailable`/1 for an unbuilt
   backend). Preserve them.

`CLAUDE.md` documents the architecture in more depth — it's worth a read before a
non-trivial change.

## Style

There's no linter or formatter configured — match the surrounding code:
`from __future__ import annotations`, type hints, dataclasses, clear names over
clever ones. Reads are not gated; writes are. Keep tests close to the behavior
they describe.

## Pull requests

- Keep the diff focused; one idea per PR.
- Add or update tests — `pytest` should pass, and new behavior should be covered.
- If you change a public behavior, update the README and `CLAUDE.md` to match.
- Describe *why*, not just *what*, in the PR body.

Small fixes and docs improvements are very welcome. For larger changes (a new
backend, a new logged operation, a change to the gate), open an issue first so we
can agree on the shape before you build it.
