# CLAUDE.md — Project Rules for pyleans

## ⚠️ Rule 0 — Working directory: NEVER use `cd`

**The shell's working directory is ALWAYS the repository top level (`c:\Heiner\github-pyleans`). Every `Bash` tool call starts there. Do not change it.**

- ❌ **NEVER** prefix a command with `cd <anywhere> && ...`. Not `cd c:/Heiner/github-pyleans && pytest`. Not `cd src && python -m counter_app`. Not even "just this once."
- ❌ **NEVER** use `cd` to "make sure" you're in the right place. You are. The harness guarantees it.
- ✅ Run commands bare: `pytest`, `git status`, `python -m src.counter_app`.
- ✅ For paths outside cwd, pass the path as an argument: `pytest src/counter_app/test`, `ls src/counter_app/`.

The **only** legitimate `cd` is when a command genuinely cannot accept a path argument AND the behavior depends on cwd. That is vanishingly rare. If you are about to type `cd`, stop and find another way.

Why: `cd <repo> && ...` bypasses the harness's permission matcher (rules are keyed on the bare command), which forces unnecessary approval prompts and creates inconsistent command strings across the session.

## Project Overview

**pyleans** is a Python implementation of the Virtual Actor pattern (inspired by Microsoft Orleans).
It uses Hexagonal Architecture with pluggable providers for membership, storage, and streaming.

## Architecture & Design Decisions

All project documentation lives under [docs/](docs/):

- [docs/plan.md](docs/plan.md) — implementation plan: scope, phase roadmap, package structure.
- [docs/adr/](docs/adr/) — Architecture Decision Records (MADR format), one decision per file. Index: [docs/adr/README.md](docs/adr/README.md).
- [docs/architecture/](docs/architecture/) — long-form architecture specs (e.g. [pyleans-transport.md](docs/architecture/pyleans-transport.md)).
- [docs/domain/](docs/domain/) — ubiquitous-language glossary for the problem space, one concept per file: `NNNN-<kebab-name>.md`. Index: [docs/domain/README.md](docs/domain/README.md).
- [docs/tasks/](docs/tasks/) — per-phase task specs. Overview in [docs/tasks/README.md](docs/tasks/README.md); each task is a `task-PP-NN-name.md` file (PP = 2-digit phase number, NN = 2-digit task number within that phase).
- [docs/orleans-architecture/](docs/orleans-architecture/) — Orleans reference documentation used while designing pyleans.
- [docs/orleans-sample/](docs/orleans-sample/) — Orleans sample app (C# demo, see [docs/orleans-sample/README.md](docs/orleans-sample/README.md)).
- [docs/papers/](docs/papers/) — background papers (PDFs plus [hexagonal-architecture.md](docs/papers/hexagonal-architecture.md) and [beyond-stateless-talk.md](docs/papers/beyond-stateless-talk.md)).

### Implementation state

**Phase 1 (single silo, dev mode)** is being implemented. Tasks are in [docs/tasks/README.md](docs/tasks/README.md).

### Transport design

Pluggable transport layer documented in [docs/architecture/pyleans-transport.md](docs/architecture/pyleans-transport.md). The pluggability decision is in [adr-cluster-transport](docs/adr/adr-cluster-transport.md).
Phase 1 does not include networking. Phase 2 adds TCP mesh transport.

## Mandatory Coding Standards

Every contributor (human or AI) must follow the project coding standards. No exceptions without explicit approval.

**See [.claude/coding-rules.md](.claude/coding-rules.md)** for the full rules: Clean Code, SOLID, DRY, YAGNI, KISS, composition over inheritance, fail fast, Law of Demeter, strict type hints, and hexagonal architecture — plus the reviewer checklist.

The rules apply in three contexts, all equally binding:

1. **Production and test code** under [src/](src/).
2. **Pseudo-code / example code in task specs** under [docs/tasks/](docs/tasks/). Sketches omit bodies, never types or shape.
3. **Example code in ADRs and architecture docs** under [docs/adr/](docs/adr/) and [docs/architecture/](docs/architecture/). Sketches here set the vocabulary the implementation uses.

Read the rules file before writing code in any of these three contexts.

## Logging Requirements — MANDATORY

**All significant activity must be logged. No feature is complete without logging.**

Every operation that changes state, crosses a boundary, or could fail must emit a
log message. Logging is not optional or an afterthought — it is as mandatory as tests.

### Logging Standards

- Use `logging.getLogger(__name__)` in every module. Grains use `logging.getLogger(f"pyleans.grain.{grain_type}")`.
- Follow the log level guideline from [adr-logging](docs/adr/adr-logging.md):
  - **INFO**: lifecycle events, ≤1/sec per module or grain (activation, deactivation, silo start/stop, membership changes)
  - **DEBUG**: per-operation, frequent (grain calls, `write_state`, storage I/O, gateway messages, timer ticks)
  - **WARNING**: unexpected but recoverable (exceptions caught, etag conflicts, timeouts)
  - **ERROR**: operation failures (activation failure, provider errors, unhandled exceptions)
- Rule of thumb: if a log line fires more than once per second per module or grain instance, it is DEBUG.
- Log messages must be actionable: include the grain identity, method name, or operation context.
- Never log secrets, credentials, or full grain state payloads.

## Testing Requirements — MANDATORY

**Every feature must have unit tests. No feature is complete without tests.**

### Coverage Rules

All tests must cover:

1. **Happy path**: The normal, expected usage with valid inputs.
2. **Equivalence classes**: At least one test per distinct class of valid input (e.g., empty collection, single item, many items).
3. **Boundary values**: Edges of valid ranges (zero, one, max, min, empty string, etc.).
4. **Error cases**: Invalid inputs, missing data, null/None values — verify correct exceptions or error handling.
5. **Edge cases**: Concurrency scenarios (where applicable), re-entrant calls, lifecycle transitions, timeout behavior.

### Testing Standards

- Use **pytest** as the test framework.
- Tests must be fast, isolated, and deterministic — no external service dependencies in unit tests.
- Use in-memory fakes or test doubles (not mocks unless necessary) for driven port adapters.
- Test file naming: `test_<module>.py` in the `test/` directory mirroring the package structure.
- Each test function tests one behavior and has a descriptive name: `test_<what>_<condition>_<expected>`.
- **Arrange / Act / Assert structure in every test, with explicitly labeled section comments** (`# Arrange`, `# Act`, `# Assert`). Exactly one Act step per test.
- Tests are first-class code — they follow the same quality standards as production code.

**See [.claude/testing-rules.md](.claude/testing-rules.md)** for the full rules: the AAA labeling contract, the unit / integration / smoke-test scopes, fakes-vs-mocks guidance, fixture placement, determinism rules, anti-patterns, and the pre-commit checklist.

### When Tests Can Be Skipped

Never. If you think a piece of code doesn't need tests, you are wrong. Even trivial code gets tested — it documents expected behavior and catches regressions.

## Python Project Conventions

- **Python version**: 3.12+
- **Package manager**: pip with venv
- **Build backend**: hatchling
- **Formatting**: ruff (format + lint)
- **Additional linter**: pylint (strict, tuned via `[tool.pylint.*]` in pyproject.toml)
- **Test runner**: pytest
- **Type checker**: mypy in strict mode
- **Project layout**: flat layout with `src/` and `test/` per package
- **One grain per file**: every grain class gets its own file, named in snake_case (`CounterGrain` → `counter_grain.py`). State dataclass lives in the same file. Test-only grains are exempt.

### Common Commands

```bash
python -m venv .venv             # create virtual environment
.venv/Scripts/activate           # activate (Windows)
# source .venv/bin/activate      # activate (Linux/macOS)
pip install -e ".[dev]"              # install pyleans + dev deps in editable mode
pytest                               # run all tests across workspace
pytest src/pyleans/test              # run only pyleans tests
pytest src/counter_app/test          # run only counter-app tests
mypy src/pyleans/pyleans             # type-check pyleans
ruff check .                     # lint everything
ruff format .                    # format everything
pylint src/pyleans/pyleans src/counter_app src/counter_client   # strict lint (must be 10/10)
```

## Package Relationships

- `pyleans` is the framework library (no CLI entry point)
- `src/counter_app` is a sample silo app (run as `python -m src.counter_app`, no pip install needed)
- `src/counter_client` is a sample CLI that talks to counter_app via `pyleans.client` (run as `python -m src.counter_client`)
- `pyleans.server` is the silo runtime (import only in silo processes)
- `pyleans.client` is the lightweight client (import in external apps)
- `pyleans.gateway` is the TCP gateway protocol (used by both server and client)
- Shared code (grain.py, identity.py, etc.) lives in the `pyleans` package at `src/pyleans/pyleans/`

### Running the applications

All apps are run as Python modules — no installed console scripts.

```bash
python -m src.counter_app                          # start silo (blocks, Ctrl+C to stop)
python -m src.counter_client get my-counter        # CLI client
python -m src.counter_client inc my-counter
python -m src.counter_client set my-counter 42
```

## Post-Task Reviews — MANDATORY

After completing each task (code + tests passing), you MUST perform both reviews below **before committing**. Do NOT batch reviews across multiple tasks. Do NOT skip reviews for "simple" tasks. Every task gets both reviews, every time.

### Step-by-step workflow per task

1. Implement code + tests
2. Run tests — all must pass
3. **Code review** (see checklist below) and put the findings as open issues into the task file.
4. **Security review** (see checklist below) and put the findings as open issues into the task file.
5. Fix all issues found in steps 3–4 and update the issues in the task file.
6. Re-run tests to confirm fixes don't break anything
7. Run `ruff check .` and `ruff format --check .` — fix all lint and formatting issues
8. Run `pylint src` — must score 10.00/10. Fix every finding; do not add blanket disables to the config to silence new warnings. If a specific finding is genuinely a false positive or a deliberate exception, use a narrowly-scoped `# pylint: disable=<code>` comment with a short justification on the same line or the line above.
9. Run `mypy .` — fix all type errors
10. Add a summary of changes to the task file
11. Only then: commit

### Code Review Checklist

Review all code written or modified in the task for:

- Adherence to the rules in [.claude/coding-rules.md](.claude/coding-rules.md) (clean code, SOLID, DRY, YAGNI, KISS)
- Correct use of type hints, error handling, and naming
- Test quality and completeness per [.claude/testing-rules.md](.claude/testing-rules.md) (all acceptance criteria covered, AAA labeling, fakes-vs-mocks)
- Architectural consistency with hexagonal architecture
- No dead code, unused imports, or magic constants
- Sketches added or modified in [docs/tasks/](docs/tasks/) and [docs/adr/](docs/adr/) also meet the coding rules

Fix all issues found before proceeding.

### Security Review Checklist

Review the **entire existing codebase** (not just the current task) for:

- OWASP Top 10 vulnerabilities
- Path traversal, injection, insecure deserialization
- Improper input validation at system boundaries
- Unsafe file operations, race conditions
- Dependency vulnerabilities
- Unbounded resource consumption (queues, strings, collections)

Fix all vulnerabilities found. Add tests for any security fix.

## Commit and PR Rules

- Every commit must leave the project in a working state (tests pass, type checks pass).
- **Commit messages are reviewed at the git-commit permission prompt — they are the primary artefact the human uses to see what happened.** Write them for that reader, not for future archaeology.
- Commit message format is strict:
  - Subject line: imperative mood, ≤72 characters, describes the headline change.
  - Body is mandatory and is a flat bullet-point list — no headings, no sub-headings, no bold/italic styling, no grouping. One bullet per distinct change.
  - Each bullet states what changed and why, in one line. The diff already shows line-level detail; the bullet summarises the meaning of that diff.
  - Call out anything a reviewer would want to block on: removed legacy surface, renamed public symbols, changed on-wire / on-disk formats, new dependencies, new `type: ignore`s, touched public APIs, changed tests, changed rules in `.claude/`.
  - Do not include: file-path dumps with no commentary, counts of tests passing, lint-score restatements ("pylint 10/10"), "Task XX-YY" prefixes in the body (they belong only in the subject), or generated-content-style blurbs.
- Do not commit generated files, secrets, or IDE-specific configuration.
- After completing each task: perform code review, security review, fix all issues from both reviews, verify all unit tests pass, then git commit the result.
