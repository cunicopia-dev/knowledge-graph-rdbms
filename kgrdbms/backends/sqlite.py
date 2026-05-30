"""SQLite engine — the live default, and the identity of the project.

Zero dependencies, in-process, one file. The existing `Graph` already *is* a
`GraphBackend` (structurally), so this factory is a thin adapter: ensure the
parent dir exists, open the file. Its event log co-locates in the same file
(see resolver), which is what makes the default ontology fully embeddable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from kgrdbms.backends import backend
from kgrdbms.backends.base import GraphBackend
from kgrdbms.graph import Graph


@backend("sqlite")
def open_sqlite(*, location: str, **options: Any) -> GraphBackend:
    if location not in (":memory:", ""):
        Path(location).parent.mkdir(parents=True, exist_ok=True)
    return Graph(path=location)
