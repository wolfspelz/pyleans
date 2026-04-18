# Architecture Decision Records

This directory holds the ADRs (Architecture Decision Records) for pyleans. Each file captures one decision: its context, the options considered, and the outcome with consequences.

## Format

MADR-style Markdown with YAML frontmatter (`status`, `date`, `tags`). Background:

- [Michael Nygard, "Documenting Architecture Decisions"](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
- [MADR (Markdown Architectural Decision Records)](https://adr.github.io/madr/)

Filenames are `adr-<kebab-name>.md` — unnumbered. When a decision is revisited, append a new ADR with `supersedes:` / `superseded-by:` in frontmatter; do not rewrite the old one.

### Statuses

`proposed` → `accepted` → (`deprecated` | `superseded`)

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
- [Pluggable Transport — Start with TCP, Add Others Later](adr-cluster-transport.md)
- [Cluster Access Boundary — Gateway Protocol Is the Sole External Entry Point](adr-cluster-access-boundary.md)

### Topology and packaging

- [Dev Mode — Single-Silo, In-Process](adr-dev-mode.md)
- [pyleans Is a Library, Not a CLI Tool](adr-library-vs-cli.md)
- [One Package, Two Entry Points (`server` / `client`)](adr-package-split.md)

### Cross-cutting

- [Naming Convention](adr-naming-convention.md)
- [Logging](adr-logging.md)
- [Graceful Shutdown](adr-graceful-shutdown.md)
