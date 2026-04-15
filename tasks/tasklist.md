# Pyleans Task List

Tasks ordered by dependency. Each task can start when all its dependencies are complete.

## Rules for Every Task

1. **Follow CLAUDE.md coding standards**: Clean code, SOLID, DRY, YAGNI, KISS, strict type hints, hexagonal architecture. See [CLAUDE.md](../CLAUDE.md) for the full list.
2. **Tests are mandatory**: Every task must include unit tests. No feature is complete without tests. See CLAUDE.md "Testing Requirements" section.
3. **Update the task file on completion**: When a task is done, fill in the "Summary of implementation" section at the bottom of the task file with:
   - Files created/modified (with paths)
   - Key implementation decisions made during coding
   - Any deviations from the original design and why
   - Test coverage summary
4. **Mark the task as done** in this tasklist by changing `[ ]` to `[x]`.

## Dependency Graph

```
01-project-setup
  |
  +---> 02-core-types ---+---> 05-provider-abcs --+---> 10-file-storage
  |         |             |          |              +---> 11-yaml-membership
  |         |             |          |              +---> 13-in-memory-streaming --+
  |         +-------------+----------+                                            |
  |         |                                                                     |
  +---> 03-errors -------> 04-serialization                                       |
                               |                                                  |
                               +---> 06-grain-decorator                           |
                               |         |                                        |
                               +---------+---> 07-grain-runtime --+               |
                                                    |             |               |
                                                    +---> 08-grain-reference      |
                                                    |         |                   |
                                                    +---> 12-grain-timers         |
                                                    |                             |
                                                    +---> 09-di-container         |
                                                              |                   |
                                                              +---+---+---+---+---+
                                                              |
                                                          14-silo
                                                              |
                                                          15-counter-grain
                                                              |
                                                          16-counter-app
                                                              |
                                                          17-counter-client
```

## Tasks

### Layer 1: Foundation (no dependencies between these)

| # | Task | Status | Dependencies |
|---|---|---|---|
| 01 | [Project Setup](task-01-project-setup.md) | [x] | None |

### Layer 2: Core Types (depends on 01)

| # | Task | Status | Dependencies |
|---|---|---|---|
| 02 | [Core Identity Types](task-02-core-types.md) | [x] | 01 |
| 03 | [Error Types](task-03-errors.md) | [x] | 01 |

### Layer 3: Serialization and ABCs (depends on 02, 03)

| # | Task | Status | Dependencies |
|---|---|---|---|
| 04 | [Serialization](task-04-serialization.md) | [x] | 01, 03 |
| 05 | [Provider ABCs](task-05-provider-abcs.md) | [x] | 02, 03 |

### Layer 4: Grain System (depends on 04, 05)

| # | Task | Status | Dependencies |
|---|---|---|---|
| 06 | [Grain Decorator](task-06-grain-decorator.md) | [x] | 02, 04 |
| 10 | [File Storage Provider](task-10-file-storage.md) | [ ] | 04, 05 |
| 11 | [YAML Membership Provider](task-11-yaml-membership.md) | [ ] | 02, 05 |
| 13 | [In-Memory Stream Provider](task-13-in-memory-streaming.md) | [ ] | 05, 07 |

### Layer 5: Runtime (depends on Layer 4)

| # | Task | Status | Dependencies |
|---|---|---|---|
| 07 | [Grain Runtime](task-07-grain-runtime.md) | [x] | 03, 04, 05, 06 |

### Layer 6: Runtime Consumers (depends on 07)

| # | Task | Status | Dependencies |
|---|---|---|---|
| 08 | [Grain Reference](task-08-grain-reference.md) | [ ] | 02, 04, 07 |
| 09 | [DI Container](task-09-di-container.md) | [ ] | 05, 07, 08 |
| 12 | [Grain Timers](task-12-grain-timers.md) | [ ] | 07 |

### Layer 7: Silo Assembly (depends on all above)

| # | Task | Status | Dependencies |
|---|---|---|---|
| 14 | [Silo](task-14-silo.md) | [ ] | 07, 08, 09, 10, 11, 12, 13 |

### Layer 8: Sample Application

| # | Task | Status | Dependencies |
|---|---|---|---|
| 15 | [Counter Grain](task-15-counter-grain.md) | [ ] | 06, 14 |
| 16 | [Counter App (Silo + FastAPI)](task-16-counter-app.md) | [ ] | 10, 11, 14, 15 |
| 17 | [Counter Client (CLI)](task-17-counter-client.md) | [ ] | 16 |

## Parallel Execution Opportunities

Tasks within the same layer can be implemented in parallel:

- **Layer 2**: Tasks 02, 03 (parallel)
- **Layer 3**: Tasks 04, 05 (parallel)
- **Layer 4**: Tasks 06, 10, 11 (parallel; 13 waits for 07)
- **Layer 6**: Tasks 08, 09, 12 (parallel after 07)
