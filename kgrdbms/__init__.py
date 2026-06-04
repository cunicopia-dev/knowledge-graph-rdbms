"""kgrdbms — a label property graph on an RDBMS (SQLite).

A small, dependency-free knowledge-graph core:

    * a label property graph (nodes, typed directed edges, labels, JSON props)
      backed by SQLite — no external graph database required
    * an append-only, replayable event log with compensation (undo-as-event)
    * a two-layer mutation gate: compiled-in invariants + configurable policy
    * an optional MCP server exposing the graph to any MCP-aware client
      (install the `mcp` extra)
"""

from __future__ import annotations

__version__ = "0.1.3"

from kgrdbms.graph import Edge, Graph, Node, default_graph_path, slug
from kgrdbms.events import (
    EventLog,
    GraphEvent,
    apply_event,
    edge_spec,
    node_spec,
    replay,
)
from kgrdbms.policy import Decision, MutationContext, mutation_check
from kgrdbms.invariants import InvariantViolation, enforce

__all__ = [
    "__version__",
    # graph
    "Graph",
    "Node",
    "Edge",
    "slug",
    "default_graph_path",
    # events
    "EventLog",
    "GraphEvent",
    "apply_event",
    "replay",
    "node_spec",
    "edge_spec",
    # policy / invariants
    "MutationContext",
    "Decision",
    "mutation_check",
    "InvariantViolation",
    "enforce",
]
