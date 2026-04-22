# Task PP-NN: <Task Title>

<!-- Filename: task-PP-NN-<slug>.md  (PP = 2-digit phase, NN = 2-digit task-within-phase, starting at 01 per phase). -->

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly -- clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-PP-NN-<name>.md](task-PP-NN-<name>.md)
- <!-- one bullet per upstream task this task depends on; delete section if none -->

## References
- [adr-<slug>](../adr/adr-<slug>.md)
- [plan.md](../plan.md) -- <phase/item reference>
- <!-- add orleans-architecture/ or architecture/ docs when they informed the design -->

## Description

<One- or two-paragraph summary of what this task delivers and why it is needed.
Link back to the ADR(s) for the "why" rather than restating the decision here.>

### Motivation

<!-- Optional. Include only when the task removes boilerplate, resolves a specific
pain point, or otherwise has a rationale that isn't obvious from the description.
Delete the subsection entirely if not needed. -->

### Files to create/modify

**Create:**
- `src/<package>/<path>.py` -- <one-line purpose>

**Modify:**
- `src/<package>/<path>.py` -- <one-line reason>
- Tests for all created/modified files

<!-- If the task only creates new files, replace this section with a simpler
"### Files to create" list. -->

### Design

```python
# src/<package>/<path>.py
# Minimal sketch of the public shape: class signatures, key methods, type hints.
# Keep this to the API surface -- implementation details belong in the code.
```

<!-- Add further ### subsections for any non-trivial design aspect the implementer
needs to get right: concurrency invariants, lifecycle ordering, integration with
other runtime components, error handling strategy, etc. Each subsection should
answer a question an implementer would otherwise have to guess at. -->

### Acceptance criteria

- [ ] <Observable behavior 1 -- phrased as a testable outcome, not an implementation step>
- [ ] <Observable behavior 2>
- [ ] Unit tests cover happy path, equivalence classes, boundaries, error cases, and edge cases (per CLAUDE.md)
- [ ] `ruff check .`, `ruff format --check .`, `pylint src` (10.00/10), `mypy .` all clean
- [ ] All existing tests still pass

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation

### Overview
<A short prose paragraph explaining what was actually
delivered. Start from the task goal, describe the shape of the solution, and
call out anything a reviewer needs to know before reading the diff. The bullet
lists below give the itemized detail; this section gives the narrative. Write
in past tense -- this captures what was done, not what is planned.>

### Files created
- `<path>` -- <one-line description>

### Files modified
- `<path>` -- <one-line description>

### Key decisions
- <Decision 1 and the reason behind it>
- <Decision 2>

### Deviations
- <Any departure from the design above, with justification. Write "None." if there were none.>

### Test coverage
- <N tests: brief breakdown of what they cover (happy path, error cases, edge cases, etc.)>
