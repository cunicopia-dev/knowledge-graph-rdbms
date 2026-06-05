"""The backbone — the thin shared layer that binds independent ontologies.

Each ontology is its own file with FK-bound edges, so a *leaf* edge can never
cross ontologies (its foreign key lives in one file). The backbone is where
cross-ontology structure lives instead, and it needs no new storage: the index
(`<root>/index.db`, the "database of databases") is already a kg, so the
backbone is just additional *kinds* inside it.

Two jobs:

  1. **Cross-ontology links (Layer 2).** A typed edge whose endpoints live in
     different ontologies. Endpoints are lightweight `Ref` proxy nodes inside the
     index, addressed by an *ontology-qualified id* `<ontology>::<node_id>`, so
     the edge's foreign keys are satisfied within `index.db`. `SAME_AS` is the
     special link that asserts two leaf nodes are the same real-world entity.

  2. **The prefix registry (Layer 3).** CURIE prefix -> IRI base — "the lookup
     table you only need the day you publish." Promoting it to a first-class
     artifact (one `Prefix` node per prefix) is the lightweight identity backbone:
     no upper ontology, just shared addresses for the entities that span domains.

Every backbone write goes through the gated + logged `service` path against the
index's own event log, so cross-ontology assertions are audited, reversible, and
replayable just like leaf-graph data. Reads hit the index `Graph` directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kgrdbms import resolver, service
from kgrdbms.events import EventLog
from kgrdbms.graph import Graph, Node

# ---- qualified ids: how an endpoint names a node in another ontology ----

QUALIFIER = "::"  # `<ontology>::<node_id>` — distinct from the single-colon CURIE
REF_KIND = "Ref"
PREFIX_KIND = "Prefix"
SAME_AS = "SAME_AS"


def qualify(ontology: str, node_id: str) -> str:
    """`("coffee", "drink:latte") -> "coffee::drink:latte"`. The `::` separates the
    ontology namespace from the node's own (single-colon) CURIE."""
    return f"{ontology}{QUALIFIER}{node_id}"


def unqualify(qualified_id: str) -> tuple[str, str]:
    """`"coffee::drink:latte" -> ("coffee", "drink:latte")`. Splits on the first
    `::` only, so the node's own CURIE colons are preserved."""
    ontology, _, node_id = qualified_id.partition(QUALIFIER)
    return ontology, node_id


@dataclass
class Link:
    """One cross-ontology relationship, as seen from a node: the edge type, the
    direction, and the node on the other side (which ontology, which id)."""

    direction: str            # "out" (this -> other) or "in" (other -> this)
    type: str
    other_ontology: str
    other_id: str
    properties: dict[str, Any]


# ---- opening the backbone (it IS the index graph) --------------------


def _open(root: str | Path | None = None) -> tuple[Graph, EventLog]:
    idx = resolver._open_index(root)
    return idx, EventLog(idx)


def _ensure_ref(idx: Graph, events: EventLog, ontology: str, node_id: str, actor: str) -> str:
    """Upsert the proxy node for a leaf entity and return its qualified id."""
    qid = qualify(ontology, node_id)
    service.upsert_node(
        idx, events, id=qid, kind=REF_KIND, name=node_id,
        labels=[REF_KIND], properties={"ontology": ontology, "ref": node_id}, actor=actor,
    )
    return qid


# ---- Layer 2: cross-ontology links -----------------------------------


def link(
    from_ontology: str,
    from_id: str,
    type: str,
    to_ontology: str,
    to_id: str,
    *,
    properties: dict[str, Any] | None = None,
    symmetric: bool = False,
    actor: str = "backbone",
    root: str | Path | None = None,
) -> dict:
    """Assert a typed relationship between nodes in two different ontologies.

    Creates (idempotently) a `Ref` proxy for each endpoint inside the index and
    an edge between them — gated + logged like any mutation. `symmetric=True`
    also writes the reverse edge (use it for mutual relations such as SAME_AS).
    Returns {from, to, type, symmetric}.
    """
    idx, events = _open(root)
    try:
        fq = _ensure_ref(idx, events, from_ontology, from_id, actor)
        tq = _ensure_ref(idx, events, to_ontology, to_id, actor)
        service.add_edge(idx, events, fq, tq, type, properties=properties, actor=actor)
        if symmetric:
            service.add_edge(idx, events, tq, fq, type, properties=properties, actor=actor)
        return {"from": fq, "to": tq, "type": type, "symmetric": symmetric}
    finally:
        idx.close()


def same_as(
    from_ontology: str,
    from_id: str,
    to_ontology: str,
    to_id: str,
    *,
    actor: str = "backbone",
    root: str | Path | None = None,
) -> dict:
    """Assert two leaf nodes are the same real-world entity (a symmetric link)."""
    return link(from_ontology, from_id, SAME_AS, to_ontology, to_id,
                symmetric=True, actor=actor, root=root)


def unlink(
    from_ontology: str,
    from_id: str,
    type: str,
    to_ontology: str,
    to_id: str,
    *,
    symmetric: bool = False,
    actor: str = "backbone",
    root: str | Path | None = None,
) -> dict:
    """Remove a cross-ontology link (the reverse too when symmetric). The Ref
    proxies are left in place — they may anchor other links."""
    idx, events = _open(root)
    try:
        fq, tq = qualify(from_ontology, from_id), qualify(to_ontology, to_id)
        r = service.remove_edge(idx, events, fq, tq, type, actor=actor)
        removed = r["removed"]
        if symmetric:
            removed += service.remove_edge(idx, events, tq, fq, type, actor=actor)["removed"]
        return {"removed": removed}
    finally:
        idx.close()


def links_of(
    ontology: str, node_id: str, *, type: str | None = None, root: str | Path | None = None
) -> list[Link]:
    """Every cross-ontology link touching a leaf node (both directions)."""
    idx, _ = _open(root)
    try:
        qid = qualify(ontology, node_id)
        if idx.node(qid) is None:
            return []
        out: list[Link] = []
        for edge, other in idx.out(qid, type):
            o_ont, o_id = unqualify(other.id)
            out.append(Link("out", edge.type, o_ont, o_id, edge.properties))
        for edge, other in idx.in_(qid, type):
            o_ont, o_id = unqualify(other.id)
            out.append(Link("in", edge.type, o_ont, o_id, edge.properties))
        return out
    finally:
        idx.close()


def identity_cluster(
    ontology: str, node_id: str, *, max_depth: int = 8, root: str | Path | None = None
) -> list[tuple[str, str]]:
    """The set of leaf nodes asserted SAME_AS one another, transitively.

    Returns (ontology, node_id) pairs *including* the seed. A BFS over SAME_AS
    edges in the index — so `same_as(A, B)` then `same_as(B, C)` makes A, B, C
    one identity cluster. Explicit links only; federation folds in *implicit*
    shared-identity matches on top of this.
    """
    idx, _ = _open(root)
    try:
        seed = qualify(ontology, node_id)
        if idx.node(seed) is None:
            return [(ontology, node_id)]
        seen = {seed}
        frontier = [seed]
        for _ in range(max_depth):
            nxt: list[str] = []
            for qid in frontier:
                for _e, other in idx.out(qid, SAME_AS):
                    if other.id not in seen:
                        seen.add(other.id)
                        nxt.append(other.id)
                for _e, other in idx.in_(qid, SAME_AS):
                    if other.id not in seen:
                        seen.add(other.id)
                        nxt.append(other.id)
            frontier = nxt
            if not frontier:
                break
        return [unqualify(q) for q in sorted(seen)]
    finally:
        idx.close()


# ---- Layer 3: the prefix / IRI registry ------------------------------


def register_prefix(
    prefix: str, iri_base: str, *, actor: str = "backbone", root: str | Path | None = None
) -> dict:
    """Bind a CURIE prefix to an IRI base (e.g. `person` ->
    `https://kg.local/person/`). The lightweight identity backbone."""
    idx, events = _open(root)
    try:
        service.upsert_node(
            idx, events, id=f"prefix:{prefix}", kind=PREFIX_KIND, name=prefix,
            labels=[PREFIX_KIND], properties={"iri_base": iri_base}, actor=actor,
        )
        return {"prefix": prefix, "iri_base": iri_base}
    finally:
        idx.close()


def prefixes(root: str | Path | None = None) -> dict[str, str]:
    """All registered prefix -> IRI-base bindings."""
    idx, _ = _open(root)
    try:
        return {n.name: n.properties.get("iri_base", "") for n in idx.nodes_by_kind(PREFIX_KIND)}
    finally:
        idx.close()


def expand(curie: str, root: str | Path | None = None) -> str | None:
    """`person:ada -> https://kg.local/person/ada`, or None if the prefix is
    unregistered or the string has no prefix."""
    prefix, sep, ref = curie.partition(":")
    if not sep:
        return None
    base = prefixes(root).get(prefix)
    return base + ref if base is not None else None


def contract(iri: str, root: str | Path | None = None) -> str | None:
    """Reverse of expand: the longest registered IRI base that prefixes `iri`
    wins. Returns the CURIE, or None if no base matches."""
    best: tuple[str, str] | None = None  # (prefix, base)
    for prefix, base in prefixes(root).items():
        if base and iri.startswith(base) and (best is None or len(base) > len(best[1])):
            best = (prefix, base)
    if best is None:
        return None
    return f"{best[0]}:{iri[len(best[1]):]}"
