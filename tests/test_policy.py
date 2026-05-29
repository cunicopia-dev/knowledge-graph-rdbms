"""Policy + invariants: the two-layer mutation gate."""

from __future__ import annotations

import pytest

from kgrdbms.graph import Graph
from kgrdbms.invariants import InvariantViolation, enforce
from kgrdbms.policy import Decision, MutationContext, mutation_check


def test_default_policy_permits_everything():
    for op in ("node_upsert", "node_delete", "edge_add", "edge_remove", "graph_clear"):
        decision = mutation_check(MutationContext(operation=op))
        assert decision.allowed is True


def test_decision_helpers():
    allow = Decision.allow("ok")
    deny = Decision.deny("nope")
    assert allow.allowed and allow.reason == "ok"
    assert not deny.allowed and deny.reason == "nope"


def test_default_invariants_enforce_nothing(tmp_path):
    g = Graph(path=tmp_path / "g.db")
    # No exception for any operation under the default (no-op) invariants.
    for op in ("node_delete", "edge_remove", "graph_clear"):
        enforce(g, MutationContext(operation=op))
    g.close()


def test_invariant_violation_is_distinct_from_permission_error():
    assert not issubclass(InvariantViolation, PermissionError)


def test_a_custom_policy_can_deny(monkeypatch):
    """Demonstrate the intended extension shape: swap mutation_check's body."""
    def append_only(ctx: MutationContext) -> Decision:
        if ctx.operation in {"node_delete", "edge_remove", "graph_clear"}:
            return Decision.deny("append-only")
        return Decision.allow()

    assert append_only(MutationContext(operation="node_upsert")).allowed
    assert not append_only(MutationContext(operation="node_delete")).allowed


def test_a_custom_invariant_can_seal(tmp_path):
    """Demonstrate a compiled-in invariant sealing a node kind from deletion."""
    def my_enforce(graph, ctx: MutationContext) -> None:
        if ctx.operation == "node_delete" and ctx.node_kind == "Root":
            raise InvariantViolation("Root is the floor")

    with pytest.raises(InvariantViolation):
        my_enforce(None, MutationContext(operation="node_delete", node_kind="Root"))
    # non-root deletes pass
    my_enforce(None, MutationContext(operation="node_delete", node_kind="Leaf"))
