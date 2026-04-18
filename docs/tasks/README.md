# Pyleans Task List

Tasks ordered by dependency. Each task can start when all its dependencies are complete.

- **Phase 1** (tasks 01–18): single silo, dev mode. **Complete.**
- **Phase 2** (tasks 19–37): multi-silo cluster with pluggable transport, distributed grain directory, failure detector. **In progress.** See [plan.md §5 Phase 2](../plan.md).

## Rules for Every Task

1. **Follow CLAUDE.md coding standards**: Clean code, SOLID, DRY, YAGNI, KISS, strict type hints, hexagonal architecture. See [CLAUDE.md](../CLAUDE.md) for the full list.
2. **Tests are mandatory**: Every task must include unit tests. No feature is complete without tests. See CLAUDE.md "Testing Requirements" section.
3. **New task files use the template**: Start from [task-template.md](task-template.md) -- it defines the required sections (Dependencies, References, Description, Acceptance criteria, review findings, Summary of implementation).
4. **Update the task file on completion**: When a task is done, fill in the "Summary of implementation" section at the bottom of the task file with:
   - Files created/modified (with paths)
   - Key implementation decisions made during coding
   - Any deviations from the original design and why
   - Test coverage summary
5. **Mark the task as done** in this tasklist by changing `[ ]` to `[x]`.

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
| 07 | [Grain Base Class](task-07-grain-base-class.md) | [x] | 02, 03, 06 |
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

## Phase 2 Dependency Graph

```
                                   +--> 20-hash-ring --+----------+
                                   |                   |          |
 19-cluster-identity --+-----------+--> 21-placement --+          |
                       |                               |          |
                       |   +--> 22-transport-abcs --+--+          |
                       |   |                         |            |
                       +---+                         +--> 23-wire-protocol --+
                       |                                                     |
                       |                                24-silo-connection --+
                       |                                          |
                       |                                25-connection-manager
                       |                                          |
                       |                                26-tcp-cluster-transport ---+
                       |                                                            |
                       +--> 27-membership-extensions --+                            |
                                     |                 |                            |
                                     +--> 28-file-locking                           |
                                     |                                              |
                                     +--> 29-failure-detector <---------------------+
                                                      |                             |
                                          30-grain-directory-port                   |
                                                      |                             |
                                          31-distributed-directory <----------------+
                                                      |
                                          +-----------+-----------+
                                          |           |           |
                                 32-directory-cache  33-directory-recovery
                                          |           |
                                          +-----+-----+
                                                |
                                    34-remote-grain-invoke
                                                |
                                    35-silo-lifecycle-stages
                                                |
                                    36-multi-silo-integration-tests
                                                |
                                    37-counter-sample-multi-silo
```

### Layer 9: Phase 2 Foundation (can run in parallel after Phase 1)

| # | Task | Status | Dependencies |
|---|---|---|---|
| 19 | [Cluster Identity](task-19-cluster-identity.md) | [ ] | 02 |
| 20 | [Consistent Hash Ring](task-20-consistent-hash-ring.md) | [ ] | 19 |
| 21 | [Placement Strategies](task-21-placement-strategies.md) | [ ] | 19 |

### Layer 10: Transport -- Pluggable Inter-Silo Communication

| # | Task | Status | Dependencies |
|---|---|---|---|
| 22 | [Transport ABCs](task-22-transport-abcs.md) | [ ] | 19 |
| 23 | [Wire Protocol](task-23-wire-protocol.md) | [ ] | 19, 22 |
| 24 | [Silo Connection](task-24-silo-connection.md) | [ ] | 22, 23 |
| 25 | [Silo Connection Manager](task-25-silo-connection-manager.md) | [ ] | 23, 24 |
| 26 | [TCP Cluster Transport](task-26-tcp-cluster-transport.md) | [ ] | 22, 25 |

### Layer 11: Membership Protocol

| # | Task | Status | Dependencies |
|---|---|---|---|
| 27 | [Membership Table Extensions](task-27-membership-table-extensions.md) | [ ] | 19, 12 |
| 28 | [File Locking for Membership](task-28-file-locking-membership.md) | [ ] | 27 |
| 29 | [Failure Detector](task-29-failure-detector.md) | [ ] | 20, 26, 27 |

### Production Backends (PostgreSQL)

Production-grade backing store for Phase 2. Both providers share a single PostgreSQL instance so production deployments need only one stateful dependency.

| # | Task | Status | Dependencies |
|---|---|---|---|
| 38 | [PostgreSQL Membership Provider](task-38-postgresql-membership.md) | [ ] | 27 |
| 39 | [PostgreSQL Storage Provider](task-39-postgresql-storage.md) | [ ] | 05, 11 |

### Layer 12: Distributed Grain Directory

| # | Task | Status | Dependencies |
|---|---|---|---|
| 30 | [Grain Directory Port](task-30-grain-directory-port.md) | [ ] | 08 |
| 31 | [Distributed Grain Directory](task-31-distributed-grain-directory.md) | [ ] | 20, 21, 26, 30 |
| 32 | [Directory Cache](task-32-directory-cache.md) | [ ] | 31 |
| 33 | [Directory Recovery](task-33-directory-recovery.md) | [ ] | 29, 31 |

### Layer 13: Runtime Integration

| # | Task | Status | Dependencies |
|---|---|---|---|
| 34 | [Remote Grain Invocation](task-34-remote-grain-invoke.md) | [ ] | 26, 31, 32 |
| 35 | [Silo Lifecycle Stages](task-35-silo-lifecycle-stages.md) | [ ] | 26, 27, 29, 31, 32 |

### Layer 14: Phase 2 Validation

| # | Task | Status | Dependencies |
|---|---|---|---|
| 36 | [Multi-Silo Integration Tests](task-36-multi-silo-integration-tests.md) | [ ] | 34, 35 |
| 37 | [Counter Sample Multi-Silo](task-37-counter-sample-multi-silo.md) | [ ] | 17, 18, 34, 35, 36 |

## Parallel Execution Opportunities

Tasks within the same layer can be implemented in parallel:

- **Layer 2**: Tasks 02, 03 (parallel)
- **Layer 3**: Tasks 04, 05 (parallel)
- **Layer 4**: Tasks 06, 07, 11, 12 (parallel; 14 waits for 08)
- **Layer 6**: Tasks 09, 10, 13 (parallel after 08)
- **Layer 9**: Tasks 20, 21 (parallel after 19)
- **Layer 10**: Tasks 23, 24 sequential; 22 can start alongside 19
- **Layer 11**: Task 28 parallel with 29 (28 only blocks 27); 29 waits on 20 and 26
- **Production Backends**: Tasks 38 and 39 parallel with everything else in Phase 2; 38 only blocks on 27, 39 only blocks on Phase 1 tasks 05 and 11
- **Layer 12**: Tasks 32, 33 parallel after 31
- **Layer 13**: Tasks 34, 35 mostly independent; schedule together
