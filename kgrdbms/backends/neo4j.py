"""Neo4j engine — STUB.

Strategic role: the *escalation target* for a specific ontology whose workload
turns deep. kgrdbms's own benchmark measured the crossover — shallow point reads
favour embedded SQLite by ~30-60×, but a 1,000-deep traversal favours Neo4j's
index-free adjacency by ~76×. So this engine isn't a replacement for the default;
it's where the control plane *routes a heavy ontology* while everything else
stays embedded. Different engine per ontology, one uniform interface.

Implementation sketch (when we build it):
  * `location` is a Bolt URI (+ auth in `options`); methods compile to Cypher.
    `shortest_path` -> `shortestPath((a)-[*]-(b))`, `descendants` -> variable-
    length `MATCH`, point lookups -> `MATCH (n {id:$id})`.
  * Neo4j is its OWN source of truth, so the event log MUST live in the control
    plane (SQLite): apply each gated event to Neo4j as a projection, keep the
    log — that's how audit / replay / undo survive a non-relational backend.
    This is the seam noted in resolver._control_plane_log.
  * Mind the Bolt round-trip (~0.4ms) — it's the fixed cost the workload router
    is weighing against SQLite's in-process ~7µs.

Stubbed: `location` stored, no driver opened; every method raises via
`_StubBackend` with what's missing.
"""

from __future__ import annotations

from typing import Any

from kgrdbms.backends import backend
from kgrdbms.backends.base import GraphBackend, _StubBackend


class Neo4jGraph(_StubBackend):
    engine = "neo4j"
    # TODO: open a Bolt driver to `self.location`, and override _StubBackend's
    # methods with Cypher. Pair with a control-plane event log (see module docs).


@backend("neo4j")
def open_neo4j(*, location: str, **options: Any) -> GraphBackend:
    return Neo4jGraph(location, **options)
