# Domain

This directory holds the **ubiquitous language** for pyleans — the vocabulary of the problem space (virtual actors, grains, silos, activations, placement, membership, gateways, reminders, streams, …).

Each entry defines one concept: what it means, what is always true about it, how it relates to neighboring concepts, and where it shows up in the code. Domain entries are descriptive, not prescriptive — they say what a thing *is*, not what anyone should *do*. For decisions, see [../adr/](../adr/).

## When to add a domain entry

- A term is used in code, docs, or conversation with a specific meaning that is not obvious from general software vocabulary.
- A term has invariants worth writing down (e.g. "at most one activation per grain ID, cluster-wide").
- Two terms are easily confused and the distinction matters (e.g. *activation* vs *grain instance*).

If a term is fully defined by its type signature and a one-line docstring, it does not need an entry here.

## Format

Markdown with minimal YAML frontmatter (`status`, `tags`). Filenames are `NNNN-<kebab-name>.md` with a 4-digit zero-padded sequence number — numbers are assigned in creation order and never reused.

Start with a single file per concept. If a concept grows diagrams, worked examples, or a non-trivial set of invariants, keep it in its own file; don't split it further.

### Statuses

`active` → (`deprecated` | `renamed`)

When a concept is renamed or retired, add a new entry and mark the old one with `renamed-to:` / `deprecated` in frontmatter. Do not rewrite the old entry — incoming links stay valid.

### Template

Start from [template.md](template.md) — copy it to `NNNN-<kebab-name>.md`, fill in every section, delete placeholder text.

**Structural notes:**

- `Definition` comes **first** and is one or two sentences. A reader who only wants "what is X?" stops there.
- `Invariants` lists properties that hold at all times — the things code relies on.
- `Relationships` connects this concept to neighboring ones by link, so the reader can navigate the vocabulary.
- `In code` points at the types or modules where the concept lives, so the reader can jump to the authoritative definition.

## Index

<!-- Add one line per concept, grouped by topic. Keep each line tight. -->

### State and lifecycle

- [0001 — Grains are an active cache](0001-grains-are-an-active-cache.md) — storage is read once at activation; in-memory state is authoritative until deactivation.
