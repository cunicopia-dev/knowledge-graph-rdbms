"""The backend contract and a raising skeleton for new engines.

`GraphBackend` is the finite method surface the rest of kgrdbms depends on
(service.py for writes, the read paths for queries). Any engine that implements
it can sit behind an ontology name — SQLite today, Postgres or Neo4j tomorrow.

`_StubBackend` is a courtesy: it implements every method by raising a clear
"not implemented yet" so a new engine can be registered and *routed to*
immediately, failing loudly per-call with exactly what's missing. Subclass it,
set `engine`, and replace methods one at a time as you build the real thing.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Protocol, runtime_checkable

from kgrdbms.graph import Edge, Node


@runtime_checkable
class GraphBackend(Protocol):
    """The uniform graph surface. The interface is engine-agnostic; the *cost*
    of each call is not (a deep traversal is ~7µs of index lookups on SQLite and
    a pointer-chase under a Bolt round-trip on Neo4j) — which is exactly why the
    control plane routes by workload rather than pretending engines are fungible.
    """

    # writes — the gated path in service.py calls these
    def add_node(self, *args: Any, **kwargs: Any) -> Node: ...
    def add_label(self, node_id: str, *labels: str) -> None: ...
    def set_property(self, node_id: str, key: str, value: Any) -> None: ...
    def delete_node(self, node_id: str) -> bool: ...
    def add_edge(self, *args: Any, **kwargs: Any) -> Edge: ...
    def delete_edge(self, from_node: str, to_node: str, type: str) -> int: ...
    def incident_edges(self, node_id: str) -> list[Edge]: ...
    # reads
    def node(self, id: str) -> Node | None: ...
    def nodes_by_kind(self, kind: str) -> list[Node]: ...
    def nodes_by_label(self, label: str) -> list[Node]: ...
    def out(self, node_id: str, edge_type: str | None = ...) -> list[tuple[Edge, Node]]: ...
    def in_(self, node_id: str, edge_type: str | None = ...) -> list[tuple[Edge, Node]]: ...
    def neighborhood(self, node_id: str, depth: int = ...) -> dict[str, Node]: ...
    def shortest_path(self, from_id: str, to_id: str, max_depth: int = ...) -> list[Node] | None: ...
    def descendants(self, node_id: str, edge_type: str, max_depth: int = ...) -> list[Node]: ...
    def count_nodes_by_kind(self) -> dict[str, int]: ...
    def count_edges_by_type(self) -> dict[str, int]: ...
    def total_nodes(self) -> int: ...
    def total_edges(self) -> int: ...
    def schema(self, *, samples: bool = ..., sample_limit: int = ...) -> dict: ...
    # bulk: a context manager that defers commits to one transaction
    def batch(self) -> Any: ...
    def close(self) -> None: ...


class _StubBackend:
    """A registered-but-unbuilt engine: every method raises with what's missing.

    Satisfies `GraphBackend` structurally (all methods present), so the resolver
    will route to it and each call fails loudly rather than silently. Subclass,
    set `engine`, override as you implement.
    """

    engine = "stub"

    def __init__(self, location: str, **options: Any) -> None:
        self.location = location
        self.options = options

    def _todo(self, method: str) -> Any:
        raise NotImplementedError(
            f"{self.engine} backend: .{method}() not implemented yet "
            f"(location={self.location!r}). Override it on {type(self).__name__}."
        )

    # writes
    def add_node(self, *a: Any, **k: Any) -> Node: return self._todo("add_node")
    def add_label(self, *a: Any, **k: Any) -> None: return self._todo("add_label")
    def set_property(self, *a: Any, **k: Any) -> None: return self._todo("set_property")
    def delete_node(self, *a: Any, **k: Any) -> bool: return self._todo("delete_node")
    def add_edge(self, *a: Any, **k: Any) -> Edge: return self._todo("add_edge")
    def delete_edge(self, *a: Any, **k: Any) -> int: return self._todo("delete_edge")
    def incident_edges(self, *a: Any, **k: Any) -> list[Edge]: return self._todo("incident_edges")
    # reads
    def node(self, *a: Any, **k: Any) -> Node | None: return self._todo("node")
    def nodes_by_kind(self, *a: Any, **k: Any) -> list[Node]: return self._todo("nodes_by_kind")
    def nodes_by_label(self, *a: Any, **k: Any) -> list[Node]: return self._todo("nodes_by_label")
    def out(self, *a: Any, **k: Any) -> list[tuple[Edge, Node]]: return self._todo("out")
    def in_(self, *a: Any, **k: Any) -> list[tuple[Edge, Node]]: return self._todo("in_")
    def neighborhood(self, *a: Any, **k: Any) -> dict[str, Node]: return self._todo("neighborhood")
    def shortest_path(self, *a: Any, **k: Any) -> list[Node] | None: return self._todo("shortest_path")
    def descendants(self, *a: Any, **k: Any) -> list[Node]: return self._todo("descendants")
    def count_nodes_by_kind(self, *a: Any, **k: Any) -> dict[str, int]: return self._todo("count_nodes_by_kind")
    def count_edges_by_type(self, *a: Any, **k: Any) -> dict[str, int]: return self._todo("count_edges_by_type")
    def total_nodes(self, *a: Any, **k: Any) -> int: return self._todo("total_nodes")
    def total_edges(self, *a: Any, **k: Any) -> int: return self._todo("total_edges")
    def schema(self, *a: Any, **k: Any) -> dict: return self._todo("schema")

    @contextmanager
    def batch(self) -> Iterator["_StubBackend"]:
        """A no-op batch — writes inside still raise via _todo when attempted."""
        yield self

    def close(self) -> None:
        """Closing a never-opened stub is a no-op (keeps resolver cleanup safe)."""
        return None
