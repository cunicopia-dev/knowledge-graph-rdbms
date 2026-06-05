"""Federation — querying across many ontologies at once (Layer 1).

Each ontology is its own file, so a federated read is a *fan-out*: open each
member, run the same per-ontology read, then merge the results tagged by source.
Because SQLite (and psycopg) release the GIL during a query, the fan-out is
**multithreaded by default** — N ontologies are read concurrently on a thread
pool, and wall-clock collapses toward the slowest single ontology rather than the
sum. Each worker opens its *own* connection in its *own* thread (via
`resolver.resolve`), so no connection is ever shared across threads.

Federation is read-only. Cross-ontology *writes* (links, sameAs) are the
backbone's job (`kgrdbms.backbone`); this module only reads — including the one
identity-aware read, `node()`, which merges same-id nodes across ontologies that
opted into `shared_identity` and surfaces explicit backbone SAME_AS clusters.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from kgrdbms import backbone, resolver
from kgrdbms.graph import Node


@dataclass
class Located:
    """A node and which ontology it was found in."""

    ontology: str
    node: Node


@dataclass
class FederatedNode:
    """The cross-ontology view of one node id: every occurrence, plus a merged
    identity when shared-identity ontologies (or backbone SAME_AS) tie them."""

    id: str
    occurrences: list[Located]
    shared: list[str]            # ontologies whose copy is part of the merged identity
    merged: Node | None          # union of the shared copies, or None if identity is local


class Federation:
    """A set of ontologies queried as one. Construct with explicit names or
    `Federation.all()` for every registered ontology."""

    def __init__(
        self,
        names: list[str],
        *,
        root: str | Path | None = None,
        max_workers: int | None = None,
        parallel: bool = True,
    ) -> None:
        # Ensure each name is registered up front (in this thread), so no worker
        # thread ever takes the first-time-registration write path concurrently.
        entries = {}
        for n in names:
            entries[n] = resolver.get_entry(n, root) or resolver.register(n, root=root)
        self.names = list(names)
        self.root = root
        self._entries = entries
        self.parallel = parallel and len(self.names) > 1
        self.max_workers = max_workers or min(8, max(1, len(self.names)))

    @classmethod
    def all(cls, *, root: str | Path | None = None, **kw: Any) -> "Federation":
        names = [e.name for e in resolver.list_ontologies(root)]
        default = resolver.default_ontology_name()
        if default not in names:
            names.insert(0, default)  # the default ontology is implicit but real
        return cls(names, root=root, **kw)

    # ---- the fan-out core ------------------------------------------

    def _read_one(self, name: str, fn: Callable[[Any], Any]) -> Any:
        """Resolve one ontology in *this* thread, run `fn(backend)`, close it."""
        resolved = resolver.resolve(name, root=self.root)
        try:
            return fn(resolved.backend)
        finally:
            resolved.backend.close()

    def _fan(self, fn: Callable[[Any], Any]) -> list[tuple[str, Any]]:
        """Run `fn(backend)` across every member; return (name, result) in order.

        Concurrent by default (one thread per ontology, GIL released during the
        query); `parallel=False` runs sequentially for debugging. A member that
        raises propagates — federation does not silently drop an ontology.
        """
        if not self.parallel:
            return [(n, self._read_one(n, fn)) for n in self.names]
        with ThreadPoolExecutor(max_workers=self.max_workers) as ex:
            results = list(ex.map(lambda n: self._read_one(n, fn), self.names))
        return list(zip(self.names, results))

    def _located(self, fn: Callable[[Any], list[Node]]) -> list[Located]:
        out: list[Located] = []
        for name, nodes in self._fan(fn):
            out.extend(Located(name, n) for n in nodes)
        return out

    # ---- federated reads -------------------------------------------

    def stats(self) -> dict:
        per = self._fan(lambda b: (b.total_nodes(), b.total_edges()))
        nodes = sum(n for _, (n, _e) in per)
        edges = sum(e for _, (_n, e) in per)
        return {
            "ontologies": self.names,
            "nodes_total": nodes,
            "edges_total": edges,
            "by_ontology": {name: {"nodes": n, "edges": e} for name, (n, e) in per},
        }

    def schema(self, *, samples: bool = False) -> dict:
        """The union schema across all members, plus each member's own schema."""
        by = {name: s for name, s in self._fan(lambda b: b.schema(samples=samples))}
        return {"ontologies": self.names, "merged": _merge_schemas(by.values()), "by_ontology": by}

    def nodes_by_kind(self, kind: str) -> list[Located]:
        return self._located(lambda b: b.nodes_by_kind(kind))

    def nodes_by_label(self, label: str) -> list[Located]:
        return self._located(lambda b: b.nodes_by_label(label))

    def node(self, id: str) -> FederatedNode:
        """Find a node id across the federation (implicit identity).

        Same-id copies in ontologies that opted into `shared_identity` denote the
        same entity, so they're merged (labels unioned, properties merged). Copies
        in local-identity ontologies are reported as distinct occurrences. This
        handles *implicit* identity (same CURIE = same entity); explicit
        cross-id links use `identity()`.
        """
        occ = [Located(name, n) for name, n in self._fan(lambda b: b.node(id)) if n is not None]
        shared = [l.ontology for l in occ if self._is_shared(l.ontology)]
        merged = _merge_nodes([l.node for l in occ if l.ontology in shared]) if shared else None
        return FederatedNode(id=id, occurrences=occ, shared=shared, merged=merged)

    def identity(self, ontology: str, node_id: str) -> list[Located]:
        """The materialized SAME_AS cluster for a node: every leaf node explicitly
        asserted (transitively) to be the same real-world entity, fetched from its
        home ontology. Combines the backbone's link graph with live leaf reads.
        """
        cluster = backbone.identity_cluster(ontology, node_id, root=self.root)
        out: list[Located] = []
        for ont, nid in cluster:
            resolved = resolver.resolve(ont, root=self.root)
            try:
                n = resolved.backend.node(nid)
            finally:
                resolved.backend.close()
            if n is not None:
                out.append(Located(ont, n))
        return out

    def _is_shared(self, ontology: str) -> bool:
        entry = self._entries.get(ontology)
        return bool(entry and entry.shared_identity)


# ---- merge helpers ---------------------------------------------------


def _merge_schemas(schemas) -> dict:
    """Sum kinds / edge types / labels / per-kind keys / edge keys across members."""
    kinds: dict[str, int] = {}
    edge_types: dict[str, int] = {}
    labels: dict[str, int] = {}
    edge_keys: dict[str, int] = {}
    node_keys_by_kind: dict[str, dict[str, int]] = {}
    nodes_total = edges_total = 0
    for s in schemas:
        nodes_total += s.get("nodes_total", 0)
        edges_total += s.get("edges_total", 0)
        _accumulate(kinds, s.get("kinds", {}))
        _accumulate(edge_types, s.get("edge_types", {}))
        _accumulate(labels, s.get("labels", {}))
        _accumulate(edge_keys, s.get("edge_keys", {}))
        for kind, keys in s.get("node_keys_by_kind", {}).items():
            _accumulate(node_keys_by_kind.setdefault(kind, {}), keys)
    return {
        "nodes_total": nodes_total, "edges_total": edges_total,
        "kinds": _sorted_desc(kinds), "edge_types": _sorted_desc(edge_types),
        "labels": _sorted_desc(labels), "edge_keys": _sorted_desc(edge_keys),
        "node_keys_by_kind": {k: _sorted_desc(v) for k, v in node_keys_by_kind.items()},
    }


def _accumulate(dst: dict[str, int], src: dict[str, int]) -> None:
    for k, c in src.items():
        dst[k] = dst.get(k, 0) + c


def _sorted_desc(d: dict[str, int]) -> dict[str, int]:
    return dict(sorted(d.items(), key=lambda kv: (-kv[1], kv[0])))


def _merge_nodes(nodes: list[Node]) -> Node | None:
    """Union the labels and merge the properties of same-id copies. Later copies
    win on a property-key conflict; the kind/name come from the first copy."""
    if not nodes:
        return None
    first = nodes[0]
    labels: set[str] = set()
    props: dict[str, Any] = {}
    for n in nodes:
        labels |= n.labels
        props.update(n.properties)
    return Node(id=first.id, kind=first.kind, name=first.name, labels=labels, properties=props)
