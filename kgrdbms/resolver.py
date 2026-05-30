"""Ontology resolver — the control plane over kgrdbms backends.

Callers name an *ontology* ("ada-history"); the resolver routes that name to a
concrete `(backend, event log, entry)` bundle at the right location. Whether the
backend is a separate SQLite file, a namespaced region of a shared graph, or
(eventually) a Postgres/Neo4j instance is hidden here — the shallow interface is
`resolve(name)`; the deep module is everything below.

Two design choices make this future-proof:

  1. The return type is a `GraphBackend` **Protocol**, not the concrete SQLite
     `Graph`. The existing `Graph` satisfies it structurally (no changes there),
     and a future backend slots in by implementing the same surface. The routing
     switch in `_open_backend` is written as an explicit `elif` ladder so adding
     Neo4j is a new branch, not a rewrite.

  2. The registry of ontologies — the "database of databases" — is *itself a kg*:
     an index graph whose nodes are the ontologies. Listing ontologies is a query;
     registering one is an upsert. No new storage machinery.

The event log is constructed as a SEPARATE step from the backend. Today, for a
SQLite backend, `EventLog` shares the backend's connection and lands in the same
file. When a non-SQLite backend arrives, the log stays in the control plane (its
own SQLite) and the backend becomes a pure projection target — so audit / replay
/ undo keep working regardless of where the graph data physically lives.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

from kgrdbms.backends import GraphBackend, available_backends, get_backend
from kgrdbms.events import EventLog
from kgrdbms.graph import Graph, Node, slug

# `GraphBackend` (the engine contract) and the engine registry now live in
# `kgrdbms.backends`. The resolver is pure control plane: it maps a name to an
# entry, then asks the registry for a backend — it has no idea which engines
# exist. Re-exported here for callers that import it from the resolver.


# ---- the index entry: where mechanism meets opinion ------------------
#
# STRAWMAN — this is the seam KC flagged as "yours to shape." The fields split
# cleanly into two camps:
#
#   mechanism (the resolver reads these to ROUTE):
#       backend, path
#   opinion (the composition skill reads these to DECIDE HOW TO EXTRACT):
#       stance, id_convention, allowed_kinds
#
# An ontology thus carries its own opinion *with* it — the skill is pure
# mechanism, the ontology supplies the policy. Revise these fields freely;
# everything below treats the entry as data, so adding/removing a field is a
# one-line change here plus its use site in the (future) skill.


@dataclass
class OntologyEntry:
    name: str
    path: str                                   # backend location (db file for sqlite)
    backend: str = "sqlite"                      # routing key: sqlite | postgres | neo4j (future)
    description: str = ""
    stance: str = "literal"                      # extraction opinion: literal | inferential | ...
    id_convention: str = "kind:slug"             # how the composer mints node ids
    allowed_kinds: list[str] = field(default_factory=list)  # empty = open / unconstrained

    @classmethod
    def from_node(cls, node: Node) -> "OntologyEntry":
        p = node.properties
        return cls(
            name=node.name or node.id.split(":", 1)[-1],
            path=p["path"],
            backend=p.get("backend", "sqlite"),
            description=p.get("description", ""),
            stance=p.get("stance", "literal"),
            id_convention=p.get("id_convention", "kind:slug"),
            allowed_kinds=list(p.get("allowed_kinds", [])),
        )

    def to_properties(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("name")  # name is the node's display name, not a property
        return d


@dataclass
class Resolved:
    """What `resolve()` hands back: a routed backend, its log, and its entry."""

    backend: GraphBackend
    events: EventLog
    entry: OntologyEntry


# ---- path layout -----------------------------------------------------


def ontologies_root(root: str | Path | None = None) -> Path:
    """The control-plane root. `KGRDBMS_HOME` or ~/.kgrdbms, override per call."""
    if root is not None:
        base = Path(root)
    else:
        env = os.environ.get("KGRDBMS_HOME")
        base = Path(env).expanduser() if env else Path.home() / ".kgrdbms"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _index_path(root: str | Path | None = None) -> Path:
    return ontologies_root(root) / "index.db"


def _default_db_path(name: str, root: str | Path | None = None) -> Path:
    base = ontologies_root(root)
    # The default ontology *is* the legacy single-graph file (<root>/graph.db),
    # so naming nothing behaves exactly as the pre-resolver server/CLI did.
    # Every other ontology gets its own subdir.
    if slug(name) == slug(default_ontology_name()):
        return base / "graph.db"
    return base / "ontologies" / slug(name) / "graph.db"


def default_ontology_name() -> str:
    return os.environ.get("KGRDBMS_DEFAULT_ONTOLOGY", "default")


# ---- the index, which is itself a kg ---------------------------------


def _open_index(root: str | Path | None = None) -> Graph:
    """Open the registry graph. Its nodes ARE the ontologies."""
    return Graph(path=str(_index_path(root)))


def list_ontologies(root: str | Path | None = None) -> list[OntologyEntry]:
    idx = _open_index(root)
    try:
        return [OntologyEntry.from_node(n) for n in idx.nodes_by_kind("Ontology")]
    finally:
        idx.close()


def get_entry(name: str, root: str | Path | None = None) -> OntologyEntry | None:
    idx = _open_index(root)
    try:
        node = idx.node(f"ontology:{slug(name)}")
        return OntologyEntry.from_node(node) if node else None
    finally:
        idx.close()


def register(
    name: str,
    *,
    root: str | Path | None = None,
    path: str | None = None,
    backend: str = "sqlite",
    description: str = "",
    stance: str = "literal",
    id_convention: str = "kind:slug",
    allowed_kinds: list[str] | None = None,
) -> OntologyEntry:
    """Add (or update) an ontology in the index. Writes go direct — the registry
    is control-plane bookkeeping, not gated user data."""
    if backend != "sqlite" and not path:
        raise ValueError(
            f"backend {backend!r} requires an explicit location (a DSN), e.g. "
            f"--location 'postgresql://user:pass@host:5432/db'"
        )
    entry = OntologyEntry(
        name=name,
        path=path or str(_default_db_path(name, root)),
        backend=backend,
        description=description,
        stance=stance,
        id_convention=id_convention,
        allowed_kinds=allowed_kinds or [],
    )
    idx = _open_index(root)
    try:
        idx.add_node(
            id=f"ontology:{slug(name)}",
            kind="Ontology",
            name=name,
            labels=["Ontology"],
            properties=entry.to_properties(),
        )
    finally:
        idx.close()
    return entry


# ---- backend routing: the control-plane switch -----------------------


def _open_backend(entry: OntologyEntry) -> GraphBackend:
    """Route an entry to a live backend via the engine registry.

    The resolver names an engine; `kgrdbms.backends` owns which engines exist
    and how to open them. Unknown engines fail in `get_backend` with the list
    of what's registered; registered-but-stub engines (postgres, neo4j) open
    fine and fail loudly per-call until their methods are filled in.
    """
    return get_backend(entry.backend)(location=entry.path)


def resolve(name: str | None = None, *, root: str | Path | None = None) -> Resolved:
    """Name an ontology, get a routed `(backend, events, entry)` bundle.

    Unknown names are lazily registered with defaults, so `resolve("anything")`
    just works — the shallow interface never makes the caller pre-create things.
    """
    name = name or default_ontology_name()
    entry = get_entry(name, root) or register(name, root=root)
    backend = _open_backend(entry)
    # The log is a SEPARATE concern from the backend. For sqlite it shares the
    # backend's file (store == projection). For any non-sqlite backend the log
    # lives in a control-plane SQLite store, so audit / replay / undo survive the
    # backend swap — the backend is just the projection compensation applies to.
    if entry.backend == "sqlite":
        events = EventLog(backend)
    else:
        events = EventLog(_control_plane_log_store(entry, root), projection=backend)
    return Resolved(backend=backend, events=events, entry=entry)


class _ControlPlaneLogStore:
    """Standalone SQLite store for a non-sqlite ontology's event log.

    Provides the `(.conn, .tx())` surface `EventLog` needs, decoupled from the
    graph backend. This is the seam the neo4j/postgres stub docstrings flagged:
    the control plane owns history (in SQLite) regardless of where graph data
    physically lives.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.commit()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


def _control_plane_log_store(entry: OntologyEntry, root: str | Path | None) -> _ControlPlaneLogStore:
    """Where a non-sqlite ontology's event log lives: an `events.db` sidecar in
    that ontology's control-plane directory."""
    log_path = ontologies_root(root) / "ontologies" / slug(entry.name) / "events.db"
    return _ControlPlaneLogStore(log_path)
