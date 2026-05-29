"""Hard invariants — compiled-in mechanism, not configurable policy.

`policy.py` is policy: mutable, data-driven, swapped at configuration time.
This file is mechanism: rules enforced in code, ahead of the policy check,
regardless of who is proposing the mutation or what the policy says.

The distinction matters when the graph is exposed for live mutation. A policy
can be talked into anything by whoever controls the policy. An invariant
cannot — changing it is a code change and a redeploy, not a configuration or
a request over the wire.

The default `enforce` is a no-op: there are no built-in invariants, because
they are inherently domain-specific. Add yours below. The MCP server calls
`enforce(graph, ctx)` BEFORE `policy.mutation_check(ctx)`.
"""

from __future__ import annotations

from kgrdbms.policy import MutationContext


class InvariantViolation(Exception):
    """Raised when a mutation would breach a compiled-in invariant.

    Distinct from PermissionError (which signals a policy denial). An
    InvariantViolation cannot be configured away.
    """


def enforce(graph, ctx: MutationContext) -> None:
    """Raise InvariantViolation if `ctx` would breach a load-bearing rule.

    `graph` is passed so an invariant can inspect current state (e.g. resolve
    a node's labels for an edge endpoint). Most checks read straight off ctx.

    The default implementation enforces nothing — every mutation falls through
    to policy. Add domain rules here. For example:

        if ctx.operation == "graph_clear":
            raise InvariantViolation("the graph cannot be wiped over the wire")

        if ctx.operation == "node_delete" and ctx.node_kind == "Root":
            raise InvariantViolation("Root nodes are the floor; deletion forbidden")
    """
    return
