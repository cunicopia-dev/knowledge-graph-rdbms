# Security Policy

## Reporting a vulnerability

Please **don't** open a public issue for a security problem.

Use GitHub's private reporting: the repository's **Security** tab →
**Report a vulnerability** (Private Vulnerability Reporting). That opens a private
channel with the maintainer. If you can't use it, open a normal issue asking for
a private contact and we'll move the discussion off the public tracker.

Please include: what you found, how to reproduce it, and the impact you see.
Expect an initial response within a few days. This is a small project maintained
in spare time — thank you for your patience.

## Supported versions

The project is pre-1.0 (`0.1.x`). Fixes land on the latest release; there is no
backport guarantee for older versions yet.

## The security model (read this before assuming)

kgrdbms is **embeddable software, not a hosted service** — its security boundary
is whatever the application embedding it configures. A few things are true by
design and worth understanding so you can reason about your own deployment:

- **Two-layer mutation gate.** Logged writes (the CLI, the MCP server, anything
  through `service.py`) pass compiled-in **invariants** (`invariants.py`, run
  first, not configurable) and then a configurable **policy**
  (`policy.mutation_check`). Invariants run before policy on purpose, so a
  permissive or compromised policy can never re-open something an invariant
  sealed.

- **The default policy is permissive.** Out of the box, every mutation is
  allowed — that's the right default for a local library, and the wrong one if
  you expose the graph to an untrusted writer (e.g. an MCP server reachable by an
  agent or over a network). If you do that, **write a policy** (`policy.py` has
  worked examples) and add the invariants you can't allow to be configured away.

- **The direct path is unlogged and ungated by design.** `Graph.add_node` /
  `add_nodes` / `batch()` write straight to the projection for bulk-load speed.
  They bypass the gate and the event log. Use the `service.*` / CLI / MCP path
  when you need the audit trail and reversibility.

- **`location` is trust-sensitive.** Backend locations (a SQLite file path, a
  Postgres DSN) are opened as given. Don't let untrusted input choose an
  ontology's `location`.

- **Properties are JSON, stored as data.** They are not executed or interpolated
  into SQL (queries are parameterized), but the library does not sanitize content
  — treat stored values as untrusted on the way back out.

If you're exposing this to an AI agent over MCP, the intended posture is: a real
`policy.py`, the invariants you need compiled in, and the event log as your
audit + one-command rollback. That combination is the feature; the permissive
default is not.
