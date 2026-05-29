"""Mutation policy — the configurable gate.

When the graph is exposed for live mutation (e.g. over the MCP server), a
caller on the other end of the wire can rewrite it at runtime. That is the
feature. It is also the risk.

This module defines the policy that decides which mutations are permitted.
The default policy is PERMISSIVE: every mutation is allowed. Replace
`mutation_check` with the rules you want — five to ten lines is usually
enough to seal the parts that must not change while leaving the rest open.

Policy is *configurable*. For rules that must never be reachable regardless
of configuration, see `kgrdbms.invariants` — those are compiled-in mechanism,
checked before policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Operation = Literal[
    "node_upsert",
    "node_set_label",
    "node_remove_label",
    "node_set_property",
    "node_del_property",
    "node_delete",
    "edge_add",
    "edge_remove",
    "graph_clear",
]


@dataclass
class MutationContext:
    """All the information a policy needs to make a decision.

    Fields are populated by the caller (e.g. the MCP server) before invoking
    `mutation_check`. Any field that does not apply to the operation is None.
    """

    operation: Operation
    node_id: str | None = None
    node_kind: str | None = None
    node_labels: frozenset[str] = frozenset()
    edge_type: str | None = None
    from_node_id: str | None = None
    to_node_id: str | None = None
    property_key: str | None = None


@dataclass
class Decision:
    allowed: bool
    reason: str = ""

    @classmethod
    def allow(cls, reason: str = "") -> "Decision":
        return cls(True, reason)

    @classmethod
    def deny(cls, reason: str) -> "Decision":
        return cls(False, reason)


# ---- THE POLICY HOOK ------------------------------------------------
#
# Write your mutation policy here. The default below is fully permissive —
# every operation is allowed. Replace it with the rules you want. See the
# EXAMPLES section at the bottom for shaped suggestions.
# --------------------------------------------------------------------


def mutation_check(ctx: MutationContext) -> Decision:
    """Decide whether a mutation is permitted.

    Default: permit everything. Edit to taste. The MCP server respects every
    decision returned here.
    """
    # ---- BEGIN USER-EDITABLE POLICY ----
    return Decision.allow("default policy: everything is permitted")
    # ---- END USER-EDITABLE POLICY ----


# ---- EXAMPLES (uncomment / adapt one) -------------------------------
#
# Seal a node kind from deletion — everything else stays open:
#
#     def mutation_check(ctx: MutationContext) -> Decision:
#         if ctx.operation == "node_delete" and ctx.node_kind == "Root":
#             return Decision.deny("Root nodes cannot be deleted")
#         return Decision.allow()
#
# Append-only — callers may add, never delete or modify:
#
#     def mutation_check(ctx: MutationContext) -> Decision:
#         if ctx.operation in {"node_delete", "edge_remove", "node_remove_label",
#                              "node_del_property", "graph_clear"}:
#             return Decision.deny("policy is append-only; no deletions")
#         return Decision.allow()
#
# Seal anything carrying a "Locked" label:
#
#     def mutation_check(ctx: MutationContext) -> Decision:
#         if "Locked" in ctx.node_labels:
#             return Decision.deny("this node is locked")
#         return Decision.allow()
#
# --------------------------------------------------------------------
