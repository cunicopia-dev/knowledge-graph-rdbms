# knowledge-graph-rdbms

A label property graph on top of an ordinary RDBMS (SQLite). No graph
database, no Cypher, no external server — just five tables, a small Python
API, and an optional [MCP](https://modelcontextprotocol.io) server so any
MCP-aware client can read and mutate the graph over a wire.

The core library has **zero third-party dependencies**. The MCP server is an
optional extra.

## Why

A label property graph only needs four primitives:

| Primitive | What it is |
|-----------|------------|
| **Node** | a stable id, a kind, a display name |
| **Edge** | a typed, directed relationship between two nodes |
| **Label** | set memberships on a node (many per node) |
| **Property** | a JSON-valued key/value bag on a node or edge |

Everything else — traversal, neighborhoods, shortest paths, recursive walks —
is query ergonomics on top of those four. SQLite already gives you
transactions, indexes, durability, and recursive CTEs, so that is all this
library leans on.

## Install

```bash
pip install knowledge-graph-rdbms            # core library only
pip install "knowledge-graph-rdbms[mcp]"     # + the MCP server
```

## Quick start

```python
from kgrdbms import Graph

with Graph(path="my.db") as g:
    g.add_node("person:ada", kind="Person", name="Ada Lovelace",
               labels={"Person"}, properties={"born": 1815})
    g.add_node("field:cs", kind="Field", name="Computer Science")
    g.add_edge("person:ada", "field:cs", "FOUNDED")

    for edge, target in g.out("person:ada"):
        print(edge.type, "->", target.name)

    print(g.shortest_path("person:ada", "field:cs"))
```

Storage defaults to `~/.kgrdbms/graph.db`. Override with the `KGRDBMS_HOME`
environment variable or by passing `path=` explicitly.

## Event log: audit, undo, and time travel

The graph you query is a *projection*. An optional append-only event log is
the source of truth for every runtime mutation. Because the log never loses a
row:

- **audit is archaeology** — replay the graph to any point in time
- **undo is an event** — a reversal is a new compensating event, not a delete
- **automation is safe** — every mutation is timestamped and attributable

```python
from kgrdbms import Graph, EventLog, apply_event, replay
from kgrdbms.events import OP_NODE_UPSERT

g = Graph(path="my.db")
log = EventLog(g)

ev = log.record("alice", OP_NODE_UPSERT, {
    "after": {"id": "concept:x", "kind": "Concept", "name": "x",
              "labels": [], "properties": {}},
    "prior": None,
})
apply_event(g, ev)

log.compensate(ev.id)          # undo — emits an inverse event, keeps both rows
replay(g, log)                 # rebuild the projection from the log
replay(g, log, upto_ts=ev.ts)  # project to a past instant (time travel)
```

`replay` accepts an optional `genesis=callable` to re-seed deterministic state
(e.g. from YAML/JSON files) before the logged deltas are applied.

## Mutating safely: invariants + policy

When you expose the graph for live mutation (notably over MCP), two layers
guard every write:

1. **`kgrdbms.invariants.enforce`** — compiled-in *mechanism*. Rules here
   cannot be configured away; changing one is a code change and a redeploy.
   The default enforces nothing.
2. **`kgrdbms.policy.mutation_check`** — configurable *policy*. The default is
   permissive (everything allowed). Edit it to seal the parts that must not
   change.

Invariants are checked **before** policy, so a policy can never re-open
something an invariant has sealed.

## MCP server

```bash
pip install "knowledge-graph-rdbms[mcp]"
kgrdbms-mcp                      # stdio transport (default)
kgrdbms-mcp --transport sse
```

It exposes `kg_`-prefixed tools for reads (`kg_node_get`, `kg_nodes_by_kind`,
`kg_neighborhood`, `kg_shortest_path`, `kg_descendants`, …), gated writes
(`kg_node_upsert`, `kg_edge_add`, `kg_node_delete`, …), and the event log
(`kg_events_tail`, `kg_event_revert`, `kg_replay`). Every write passes through
the invariants + policy gate and is recorded to the event log.

## License

MIT
