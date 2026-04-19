# Coding Rules — pyleans

Detailed coding standards for pyleans. Referenced from [CLAUDE.md](../CLAUDE.md); those rules take precedence if anything here conflicts.

## When these rules apply

These rules apply **every time code appears** in the repository, regardless of where:

1. **Production and test code** — all files under `src/`.
2. **Pseudo-code and example code in task specs** — every code snippet in [docs/tasks/](../docs/tasks/) must follow the same standards (type hints, clean naming, SOLID shape). The reader cannot tell a sketch from a spec if the sketch is sloppy. Omit only *bodies* in sketches (use `...`), never *types* or *shape*.
3. **Example code in ADRs, architecture docs, and papers** — `docs/adr/`, `docs/architecture/`. Same standards. Sketches here establish the vocabulary the implementation uses; divergence between sketch and implementation is a bug in one of them.

Authors must read and apply these rules before writing code in any of those three contexts. Reviewers reject diffs that violate them.

No exceptions without explicit approval.

---

## 1. Clean Code

- Code must be readable and self-explanatory. Prefer clear naming over comments.
- Functions and methods must do one thing and do it well.
- Keep functions short — if a function needs a comment to explain a section, extract that section.
- No dead code, no commented-out code in committed files.
- Avoid magic numbers and strings — use named constants or enums.

## 2. SOLID Principles

- **Single Responsibility**: Each class/module has exactly one reason to change.
- **Open/Closed**: Classes are open for extension, closed for modification. Use abstract base classes and protocols to define extension points.
- **Liskov Substitution**: Subtypes must be substitutable for their base types without breaking behavior.
- **Interface Segregation**: Prefer small, focused interfaces (Protocols/ABCs) over large ones. Clients should not depend on methods they don't use.
- **Dependency Inversion**: High-level modules must not depend on low-level modules. Both depend on abstractions (ports). Inject dependencies; never hardcode concrete implementations.

## 3. DRY (Don't Repeat Yourself)

- Extract shared logic into a single authoritative location.
- But: three similar lines are better than a premature abstraction. Wait until duplication is proven before abstracting.

## 4. YAGNI (You Aren't Gonna Need It)

- Do not implement features, parameters, or abstractions until they are actually needed.
- Do not design for hypothetical future requirements.
- Remove unused code rather than keeping it "just in case."

## 5. KISS (Keep It Simple, Stupid)

- Choose the simplest solution that solves the problem correctly.
- Complexity must be justified by a concrete requirement, not by "what if."
- Prefer standard library solutions over third-party dependencies when equivalent.

## 6. Composition over Inheritance

- Prefer composing objects from smaller parts over deep inheritance hierarchies.
- Use inheritance only for true "is-a" relationships with shared behavior.
- Use Protocols for structural subtyping (duck typing with type safety).

## 7. Fail Fast

- Validate inputs at system boundaries and raise clear exceptions immediately.
- Do not silently swallow errors or return default values for invalid input.
- Use specific exception types, not bare `Exception`.

## 8. Law of Demeter (Principle of Least Knowledge)

- A method should only call methods on: `self`, its parameters, objects it creates, and its direct attributes.
- Do not chain through objects (`a.get_b().get_c().do_thing()`). Provide direct methods instead.
- This reduces coupling and makes code testable.

## 9. Strict Type Hints

- All public functions, methods, and class attributes must have type annotations.
- Use modern Python typing syntax (3.10+ union syntax `X | None`, 3.12+ generics where applicable).
- Use `Protocol` for structural interfaces, `ABC` for contracts with shared implementation.
- Avoid `Any` — use precise types. If `Any` is unavoidable, add a comment explaining why.
- The project uses a static type checker (mypy or pyright) — code must pass strict mode.
- **No `# type: ignore` in production code.** Fix the root cause instead: use `cast()`, `assert isinstance()`, typed helpers, or explicit attribute declarations. Test code may use `# type: ignore` where dynamic test patterns make strict typing impractical.

## 10. Remove Legacy Code

- When a subsystem evolves to a new contract, **remove the old one** — do not keep backward-compatibility shims, legacy method names, or deprecated wrappers "to avoid churn in tests or examples."
- Tests and examples that depend on the old surface are **part of the change set**: update them to the new contract in the same commit as the contract change.
- If an old method is retained, it must be because it independently belongs in the new design — not because call sites are "too many to update" or because a sample would need to change.
- Exception: when an ADR explicitly defines a migration window with a named end-state (e.g., task-01-21 was an explicit one-time migration), follow the ADR. Otherwise, never leave two ways to do the same thing.
- Rationale: legacy wrappers double the surface every consumer (including future ones) has to understand. They also hide ABI breaks that belong in the changelog. Removing old code at the point of change keeps the codebase coherent and forces reviewers to think about the migration.

## 11. Hexagonal Architecture (Ports & Adapters)

- The application core (domain logic, grain runtime, lifecycle) must have zero dependencies on frameworks, databases, or external systems.
- All external dependencies are accessed through **port interfaces** (abstract base classes or Protocols).
- Concrete implementations are **adapters** that live outside the core.
- Dependencies always point inward: adapters depend on the core, never the reverse.
- This applies to all providers: membership, storage, streaming, transport.

---

## Sketch-specific rules (task specs, ADRs, architecture docs)

When a code block appears in a task spec or ADR rather than a source file, the same ten rules above apply, with these clarifications:

- **Type hints are not optional in sketches.** A function signature without annotations teaches the wrong pattern to the next reader and invites inconsistent implementation. Even `def foo(x): ...` must be `def foo(x: int) -> str: ...`.
- **Use `...` for bodies you deliberately elide** (e.g. inside ABCs, or when the sketch is about shape not behavior). `...` is an accepted idiom and `unnecessary-ellipsis` is disabled in `pylint` config for this reason.
- **Name the same thing the same way across sketch and implementation.** If the ADR calls a parameter `network`, the task spec and the code both use `network` — never `net`, `transport`, or `io`. Spec drift between sketch and implementation is a bug; fix whichever is wrong.
- **Don't bless YAGNI violations in sketches.** If the sketch includes a hypothetical parameter, the implementer will add it. Omit it from the sketch instead.
- **Imports in sketches are illustrative, not exhaustive.** It's fine for a sketch to elide imports when they're obvious from context, but every symbol used must resolve to a real location in the codebase (or a clearly-pending one).

---

## Quick reference — what the code reviewer checks

- [ ] Single responsibility per class/module
- [ ] Dependencies injected through port abstractions, not hardcoded
- [ ] Type hints on every public signature, no `Any` without justification
- [ ] No `# type: ignore` in production code
- [ ] No dead code, commented-out code, magic numbers
- [ ] Named constants or enums for every non-trivial literal
- [ ] Functions short enough to fit on a screen (rough rule of thumb)
- [ ] No deep chain-calls across object boundaries (Law of Demeter)
- [ ] Core depends on ports only; adapters live outside the core
- [ ] Errors raised with specific types at boundaries, not swallowed
- [ ] Sketches in docs/tasks and docs/adr follow the same rules
