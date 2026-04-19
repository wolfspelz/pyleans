# Architecture Decision Records

This directory holds the ADRs (Architecture Decision Records) for pyleans. Each file captures one decision: its context, the options considered, and the outcome with consequences.

## Format

MADR-style Markdown with YAML frontmatter (`status`, `date`, `tags`). Background:

- [Michael Nygard, "Documenting Architecture Decisions"](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
- [MADR (Markdown Architectural Decision Records)](https://adr.github.io/madr/)

Filenames are `adr-<kebab-name>.md` — unnumbered. When a decision is revisited, append a new ADR with `supersedes:` / `superseded-by:` in frontmatter; do not rewrite the old one.

### Statuses

`proposed` → `accepted` → (`deprecated` | `superseded`)

### Template

Start from [adr-template.md](adr-template.md) — copy it to `adr-<kebab-name>.md`, fill in every section, delete placeholder text.

**Structural notes:**

- `Decision` comes **before** the detailed alternative analysis — a reader who only wants to know the outcome can stop after `Decision` + `Rationale`.
- `Considered Alternatives` is a short "rejected with one-line reason" list. Only expand to a full pros/cons section if an alternative is genuinely close-but-rejected and the reasoning is non-obvious.
- `Affected Components` points at the code surfaces the decision touches — the reader can jump to the relevant files.
- `Related` links to other ADRs the reader should read next or that provide context.
- When a decision is revisited, append a new ADR with `supersedes:` / `superseded-by:` in frontmatter; do not rewrite the old one.

## Index

### Grain model and runtime

- [Grain Base Class `Grain[TState]`](adr-grain-base-class.md)
- [Grain Interfaces via `@grain` decorator](adr-grain-interfaces.md)
- [State Declaration via `@grain` decorator](adr-state-declaration.md)
- [Turn-Based Concurrency Model](adr-concurrency-model.md)

### Infrastructure

- [JSON Serialization via `dataclasses` and `orjson`](adr-serialization.md)
- [Dependency Injection via `injector`](adr-dependency-injection.md)
- [Provider Interfaces as Hexagonal Ports](adr-provider-interfaces.md)
- [Grain Directory (consistent hash ring)](adr-grain-directory.md)
- [Single Activation Is a Cluster Responsibility](adr-single-activation-cluster.md)
- [Pluggable Transport — Start with TCP, Add Others Later](adr-cluster-transport.md)
- [Cluster Access Boundary — Gateway Protocol Is the Sole External Entry Point](adr-cluster-access-boundary.md)
- [Pluggable Network Port — Abstract TCP for In-Memory Test Simulation](adr-network-port-for-testability.md)

### Topology and packaging

- [Dev Mode — Single-Silo, In-Process](adr-dev-mode.md)
- [pyleans Is a Library, Not a CLI Tool](adr-library-vs-cli.md)
- [One Package, Two Entry Points (`server` / `client`)](adr-package-split.md)

### Cross-cutting

- [Naming Convention](adr-naming-convention.md)
- [Logging](adr-logging.md)
- [Graceful Shutdown](adr-graceful-shutdown.md)
