"""Gated, logged graph operations — the shared write path.

Both the MCP server and the CLI mutate the graph through this module, so the
safety gate and the event-log bookkeeping live in exactly one place.

Every mutation here:

  1. passes the compiled-in invariants (`invariants.enforce`), then the
     configurable policy (`policy.mutation_check`) — invariants first, so a
     policy can never re-open something an invariant sealed;
  2. applies the change to the graph;
  3. records a reversible event to the append-only log so replay, time-travel,
     and undo all keep working.

Reads are intentionally NOT here — callers hit the Graph directly for those.
"""

from __future__ import annotations

from typing import Any

from kgrdbms.events import (
    EventLog,
    GraphEvent,
    OP_EDGE_ADD,
    OP_EDGE_REMOVE,
    OP_NODE_DELETE,
    OP_NODE_SET_LABEL,
    OP_NODE_SET_PROPERTY,
    OP_NODE_UPSERT,
    edge_spec,
    node_spec,
    replay,
)
from kgrdbms.graph import Edge, Graph, Node
from kgrdbms import invariants, policy
from kgrdbms.policy import Decision, MutationContext

# Sentinel matching events.py: "this property did not exist before".
_MISSING = {"__missing__": True}


def guard(graph: Graph, ctx: MutationContext) -> None:
    """The gate. Invariants (compiled-in, no off switch) run BEFORE policy.

    Raises InvariantViolation (from enforce) or PermissionError (from policy).
    Both hooks are resolved through their modules at call time, so editing (or
    monkeypatching) `kgrdbms.invariants.enforce` / `kgrdbms.policy.mutation_check`
    takes effect everywhere that mutates through this service.
    """
    invariants.enforce(graph, ctx)
    decision: Decision = policy.mutation_check(ctx)
    if not decision.allowed:
        raise PermissionError(f"mutation denied by policy: {decision.reason}")


def _node_ctx(graph: Graph, node_id: str, operation: str) -> MutationContext:
    """Snapshot a node's kind/labels for a gate decision before we mutate it."""
    n = graph.node(node_id)
    return MutationContext(
        operation=operation,
        node_id=node_id,
        node_kind=n.kind if n else None,
        node_labels=frozenset(n.labels) if n else frozenset(),
    )


# ---- node mutations --------------------------------------------------


def upsert_node(
    graph: Graph,
    events: EventLog,
    *,
    id: str,
    kind: str,
    name: str | None = None,
    labels: list[str] | None = None,
    properties: dict[str, Any] | None = None,
    actor: str = "anonymous",
) -> Node:
    ctx = MutationContext(
        operation="node_upsert",
        node_id=id,
        node_kind=kind,
        node_labels=frozenset(labels or []),
    )
    guard(graph, ctx)
    prior = graph.node(id)
    prior_spec = node_spec(prior) if prior else None
    node = graph.add_node(id=id, kind=kind, name=name, labels=labels or [], properties=properties or {})
    events.record(actor, OP_NODE_UPSERT, {"after": node_spec(node), "prior": prior_spec})
    return node


def set_label(graph: Graph, events: EventLog, id: str, label: str, actor: str = "anonymous") -> Node | None:
    guard(graph, _node_ctx(graph, id, "node_set_label"))
    node = graph.node(id)
    if node is None:
        raise ValueError(f"node {id!r} does not exist")
    if label in node.labels:
        return node  # already present: a true no-op, so don't log a non-invertible event
    graph.add_label(id, label)
    events.record(actor, OP_NODE_SET_LABEL, {"id": id, "label": label})
    return graph.node(id)


def set_property(
    graph: Graph, events: EventLog, id: str, key: str, value: Any, actor: str = "anonymous"
) -> Node | None:
    ctx = _node_ctx(graph, id, "node_set_property")
    ctx.property_key = key
    guard(graph, ctx)
    prior_node = graph.node(id)
    if prior_node is None:
        raise ValueError(f"node {id!r} does not exist")
    prior_value = prior_node.properties.get(key, _MISSING)
    graph.set_property(id, key, value)
    events.record(actor, OP_NODE_SET_PROPERTY, {"id": id, "key": key, "value": value, "prior": prior_value})
    return graph.node(id)


def delete_node(graph: Graph, events: EventLog, id: str, actor: str = "anonymous") -> dict:
    guard(graph, _node_ctx(graph, id, "node_delete"))
    node = graph.node(id)
    if node is None:
        return {"deleted": False, "id": id, "edges_removed": 0}
    captured_node = node_spec(node)
    captured_edges = [edge_spec(e) for e in graph.incident_edges(id)]
    existed = graph.delete_node(id)
    events.record(actor, OP_NODE_DELETE, {"node": captured_node, "edges": captured_edges})
    return {"deleted": existed, "id": id, "edges_removed": len(captured_edges)}


# ---- edge mutations --------------------------------------------------


def add_edge(
    graph: Graph,
    events: EventLog,
    from_id: str,
    to_id: str,
    type: str,
    properties: dict[str, Any] | None = None,
    actor: str = "anonymous",
) -> Edge:
    ctx = MutationContext(operation="edge_add", edge_type=type, from_node_id=from_id, to_node_id=to_id)
    guard(graph, ctx)
    for endpoint, role in ((from_id, "from"), (to_id, "to")):
        if graph.node(endpoint) is None:
            raise ValueError(f"{role} node {endpoint!r} does not exist")
    edge = graph.add_edge(from_node=from_id, to_node=to_id, type=type, properties=properties or {})
    events.record(actor, OP_EDGE_ADD, {"edge": edge_spec(edge)})
    return edge


def remove_edge(
    graph: Graph, events: EventLog, from_id: str, to_id: str, type: str, actor: str = "anonymous"
) -> dict:
    ctx = MutationContext(operation="edge_remove", edge_type=type, from_node_id=from_id, to_node_id=to_id)
    guard(graph, ctx)
    captured = None
    for e, _n in graph.out(from_id, type):
        if e.to_node == to_id:
            captured = edge_spec(e)
            break
    removed = graph.delete_edge(from_id, to_id, type)
    if removed:
        events.record(
            actor,
            OP_EDGE_REMOVE,
            {"edge": captured or {"from": from_id, "to": to_id, "type": type, "properties": {}}},
        )
    return {"removed": removed, "from": from_id, "type": type, "to": to_id}


# ---- event log -------------------------------------------------------


def revert_event(events: EventLog, event_id: str, actor: str = "operator") -> GraphEvent:
    """Reverse an event by emitting a compensating event. The original is kept."""
    return events.compensate(event_id, actor=actor)


def replay_log(graph: Graph, events: EventLog, upto_ts: str | None = None) -> dict:
    """Rebuild the projection from the log (optionally to a past instant)."""
    return replay(graph, events, upto_ts=upto_ts)


# ---- bulk import (the shared bulk write path) ------------------------


def import_graph(
    graph: Graph,
    events: EventLog,
    *,
    nodes: list[dict] | None = None,
    edges: list[dict] | None = None,
    actor: str = "import",
) -> dict:
    """Apply many nodes then edges in ONE gated+logged transaction.

    Each item is individually gated (invariants then policy) and recorded as a
    reversible event — bulk does NOT bypass the gate. The whole thing rides one
    `batch()` so it commits once (fast) and is atomic: if any item is denied,
    the batch rolls back and nothing is written. Both `kg import` (CLI) and
    `kg_import` (MCP) are thin callers of this, so there is one bulk path, not two.

    Node specs: {id, kind, name?, labels?, properties?}.
    Edge specs: {from|from_node, to|to_node, type, properties?}.
    """
    nodes = nodes or []
    edges = edges or []
    n_nodes = n_edges = 0
    with graph.batch():
        for spec in nodes:
            upsert_node(
                graph, events,
                id=spec["id"], kind=spec["kind"], name=spec.get("name"),
                labels=list(spec.get("labels", [])), properties=dict(spec.get("properties", {})),
                actor=actor,
            )
            n_nodes += 1
        for spec in edges:
            add_edge(
                graph, events,
                spec.get("from", spec.get("from_node")), spec.get("to", spec.get("to_node")),
                spec["type"], properties=dict(spec.get("properties", {})), actor=actor,
            )
            n_edges += 1
    return {"nodes_imported": n_nodes, "edges_imported": n_edges}
