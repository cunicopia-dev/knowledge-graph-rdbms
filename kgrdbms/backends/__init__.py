"""Backend registry — the pluggable data plane.

An engine is a factory `(*, location, **options) -> GraphBackend` registered
under a name. The resolver looks the name up here and calls it; it never knows
which engines exist. Adding one is: write a module, decorate its factory with
`@backend("name")`, import it below. No switch to edit, nothing else to touch.

    from kgrdbms.backends import backend
    from kgrdbms.backends.base import GraphBackend

    @backend("myengine")
    def open_myengine(*, location: str, **options) -> GraphBackend:
        return MyEngineGraph(location, **options)
"""

from __future__ import annotations

from typing import Any, Callable

from kgrdbms.backends.base import GraphBackend, _StubBackend

BackendFactory = Callable[..., GraphBackend]

_REGISTRY: dict[str, BackendFactory] = {}


def backend(name: str) -> Callable[[BackendFactory], BackendFactory]:
    """Decorator: register a factory under an engine name."""

    def register(factory: BackendFactory) -> BackendFactory:
        _REGISTRY[name] = factory
        return factory

    return register


def get_backend(name: str) -> BackendFactory:
    """Resolve an engine name to its factory, or fail with what *is* available."""
    try:
        return _REGISTRY[name]
    except KeyError:
        have = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise ValueError(f"unknown backend {name!r}; registered engines: {have}") from None


def available_backends() -> list[str]:
    return sorted(_REGISTRY)


def open_backend(name: str, *, location: str, **options: Any) -> GraphBackend:
    """Convenience: resolve + construct in one call."""
    return get_backend(name)(location=location, **options)


# Import the engine modules so their @backend(...) registrations run. New
# engines get one line here.
from kgrdbms.backends import sqlite as _sqlite  # noqa: E402,F401
from kgrdbms.backends import postgres as _postgres  # noqa: E402,F401
from kgrdbms.backends import neo4j as _neo4j  # noqa: E402,F401

__all__ = [
    "GraphBackend",
    "_StubBackend",
    "backend",
    "get_backend",
    "available_backends",
    "open_backend",
    "BackendFactory",
]
