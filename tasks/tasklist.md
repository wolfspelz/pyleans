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
  +---> 02-core-types ---+---> 05-provider-abcs --+---> 11-file-storage
  |         |             |          |              +---> 12-yaml-membership
  |         |             |          |              +---> 14-in-memory-streaming --+
  |         +-------------+----------+                                            |
  |         |                                                                     |
  +---> 03-errors -------> 04-serialization                                       |
                               |                                                  |
                               +---> 06-grain-decorator                           |
                               |         |                                        |
                               |     07-grain-base-class                          |
                               |         |                                        |
                               +---------+---> 08-grain-runtime --+               |
                                                    |             |               |
                                                    +---> 09-grain-reference      |
                                                    |         |                   |
                                                    +---> 13-grain-timers         |
                                                    |                             |
                                                    +---> 10-di-container         |
                                                              |                   |
                                                              +---+---+---+---+---+
                                                              |
                                                          15-silo
                                                              |
                                                          16-counter-grain
                                                              |
                                                          17-counter-app
                                                              |
                                                          18-counter-client
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
| 07 | [Grain Base Class](task-07-grain-base-class.md) | [ ] | 02, 03, 06 |
| 11 | [File Storage Provider](task-11-file-storage.md) | [x] | 04, 05 |
| 12 | [YAML Membership Provider](task-12-yaml-membership.md) | [x] | 02, 05 |
| 14 | [In-Memory Stream Provider](task-14-in-memory-streaming.md) | [x] | 05, 08 |

### Layer 5: Runtime (depends on Layer 4)

| # | Task | Status | Dependencies |
|---|---|---|---|
| 08 | [Grain Runtime](task-08-grain-runtime.md) | [x] | 03, 04, 05, 06, 07 |

### Layer 6: Runtime Consumers (depends on 08)

| # | Task | Status | Dependencies |
|---|---|---|---|
| 09 | [Grain Reference](task-09-grain-reference.md) | [x] | 02, 04, 08 |
| 10 | [DI Container](task-10-di-container.md) | [x] | 05, 08, 09 |
| 13 | [Grain Timers](task-13-grain-timers.md) | [x] | 08 |

### Layer 7: Silo Assembly (depends on all above)

| # | Task | Status | Dependencies |
|---|---|---|---|
| 15 | [Silo](task-15-silo.md) | [x] | 08, 09, 10, 11, 12, 13, 14 |

### Layer 8: Sample Application

| # | Task | Status | Dependencies |
|---|---|---|---|
| 16 | [Counter Grain](task-16-counter-grain.md) | [x] | 06, 07, 15 |
| 17 | [Counter App (Standalone Silo)](task-17-counter-app.md) | [x] | 11, 12, 15, 16 |
| 18 | [Counter Client (Gateway Protocol)](task-18-counter-client.md) | [x] | 17 |

## Parallel Execution Opportunities

Tasks within the same layer can be implemented in parallel:

- **Layer 2**: Tasks 02, 03 (parallel)
- **Layer 3**: Tasks 04, 05 (parallel)
- **Layer 4**: Tasks 06, 07, 11, 12 (parallel; 14 waits for 08)
- **Layer 6**: Tasks 09, 10, 13 (parallel after 08)
