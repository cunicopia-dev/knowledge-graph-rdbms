---
name: kg-compose
description: Decompose documents or pasted context into a queryable kgrdbms ontology — extract entities and typed relationships, mint stable CURIE ids, and write them gated+logged into a named ontology via the `kg` CLI. Use when the user hands over source material (notes, docs, transcripts, research, pasted text) and wants it turned into a knowledge graph they can query, or says things like "compose this into an ontology", "build a KG from this", "extract the entities and relationships", "turn these notes into a graph".
---

# kg-compose — document → ontology

Turn unstructured source material into structured, queryable graph facts in a
named kgrdbms ontology. You are the extraction engine; the ontology supplies the
*opinion* (how aggressive to be), and every write is gated and logged so a wrong
call is reversible, not permanent.

## The one idea

**You are mechanism; the ontology is policy.** Don't impose a house style — read
the target ontology's `stance` and honor it. A `literal` legal-notes ontology and
an `inferential` research-notes ontology get *different* graphs from the same
paragraph, on purpose.

## Procedure

### 0. Resolve the target ontology
- If the user named one, use it. If not, propose a short kebab-case name from the
  material and confirm.
- Check whether it exists: `kg --json ontology list`. If absent, create it:
  `kg ontology create NAME --stance <literal|inferential> --description "…"`.
  If present, **do not recreate it** — read its existing `stance`/`path` from the
  list output and honor them.

### 1. Read the ontology's opinion
From the registry entry: `stance` (free-text extraction guidance — see *Stance*
below), `allowed_kinds` (if non-empty, prefer those kinds; extract others but
flag that they're outside the ontology's allowlist), `id_convention` (default
CURIE `prefix:slug`). These are the guidance you compose within.

### 2. Decompose the source into a `{nodes, edges}` model
- **Nodes** = the *things*. Each: `id` (a CURIE — see *Id rules*), `kind` (a
  TitleCase type like `Person`, `Company`, `Method`), `name` (display string),
  `labels` (set memberships), `properties` (JSON facts).
- **Edges** = the *relationships*. Each: `from`, `to`, `type` (UPPER_SNAKE verb
  like `FOUNDED`, `MADE_WITH`, `REPORTS_TO`), and optional `properties` (facts
  about the relationship itself — `year`, `confidence`, `source`).
- Let the **stance** govern how far past the literal text you go.

### 3. Write it (bulk, gated, logged — ONE call, not N)
Write the whole `{nodes, edges}` model in a single bulk operation. **Do not emit
dozens of individual upsert calls** — use the bulk path:

- **Over MCP:** call `kg_import(nodes=[...], edges=[...], ontology="NAME")` once.
- **Over the CLI:** `kg --ontology NAME import /tmp/compose.json --actor kg-compose`.

Both run the same gated + logged path inside one transaction — fast *and* fully
recorded, so it survives replay. Every node/edge is still individually gated and
reversible; bulk only collapses the commit, not the gate. Re-running on
overlapping sources is safe: stable CURIE ids make re-imports **merge**, never
duplicate. For a very large source, chunk into a few `kg_import` calls rather
than one per entity.

### 4. Verify and hand back the receipt
- `kg --ontology NAME stats` — what landed.
- `kg --ontology NAME path A B` (or a `neighbors`/`out` query) — show it's
  actually connected, not just a pile of nodes.
- `kg --ontology NAME events -n 10` — the audit trail of this composition.
- Summarize in prose: counts, the key entities/relationships, and anything you
  inferred (so the user can see and revert it).

## Id rules (CURIEs)
- `id = prefix:reference`. `prefix` is a short stable lowercase type token
  (`person`, `company`, `paper`); `reference` is the slugged name.
- Mint with the slug discipline: "Ada Lovelace" → `person:ada-lovelace`. Two
  spellings that slug the same **must** get the same id — that's how the same
  entity across two documents becomes one node.
- The id is an **address, not a record**: identity goes in the id, mutable facts
  go in `properties`. Never bake `status=active` into an id.

## Stance: the ontology's extraction guidance

`stance` is **free-text guidance the ontology carries about how to extract** — a
dial, not a switch, and not a fixed enum. Read it and apply judgment; the
ontology's own words win. Common values, just to convey the range (not a menu to
pick from):

- **`literal`** — assert only what the text states outright: entities only if
  named, relationships only if stated, never guess two mentions are the same.
  Fits legal, technical, contractual sources.
- **`inferential`** — also assert what's clearly implied, resolve aliases
  ("Ada" / "Lovelace" / "the Countess" → one node), normalize and enrich. Fits
  research notes, brainstorming, exploratory reading.
- …or whatever the ontology actually says — `"conservative; medical terms must
  be exact"`, `"connect aggressively, this is ideation"`. Honor the intent, not
  a keyword.

**The floor (always, regardless of stance):** when you assert something the
source didn't state outright, mark it — add `{"inferred": true}` (and a `source`
snippet when useful) to that node/edge's properties. An inference is always
visible and revertible, never laundered into a stated fact. This isn't a stance
choice; it's the one non-negotiable.

## Guardrails
- **Reversible, so don't freeze up.** Every write is logged; a bad edge is one
  `kg --ontology NAME revert <event-id>` away. Compose confidently, then review.
- **Respect `allowed_kinds`.** If the ontology constrains kinds, stay inside them
  or surface what you'd have to add.
- **Don't put data in ids.** (See *Id rules*.)
- **One ontology per coherent domain.** If the source clearly spans two unrelated
  domains, ask whether to split into two ontologies rather than blending them.

## Anti-patterns
- Minting ids by hand that bypass slug discipline (`person:Ada` vs
  `person:ada-lovelace`) — breaks cross-document dedup.
- Inventing relationships under `literal` stance.
- Encoding a whole sentence as a node `name` instead of extracting the entity.
- Emitting one write per entity — dozens of `kg_node_upsert`/`kg node add` calls —
  when a single `kg_import` (MCP) or `kg import` (CLI) does the whole batch in one
  gated, logged, atomic transaction. This is the most common mistake; don't.
