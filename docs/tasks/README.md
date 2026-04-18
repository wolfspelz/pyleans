# Pyleans Task List

Tasks ordered by dependency. Each task can start when all its dependencies are complete.

- **Phase 1** (tasks 01-01..01-18): single silo, dev mode. **Complete.**
- **Phase 2** (tasks 02-01..02-21): multi-silo cluster with pluggable transport, distributed grain directory, failure detector, production PostgreSQL backends. **In progress.** See [plan.md §5 Phase 2](../plan.md).

Task files are named `task-PP-NN-<slug>.md` where `PP` is the 2-digit phase number and `NN` is the 2-digit task number within that phase (resets to 01 each new phase).

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

## Phase 1 Dependency Graph

```
01-01-project-setup
  |
  +---> 01-02-core-types ---+---> 01-05-provider-abcs --+---> 01-11-file-storage
  |         |               |           |                +---> 01-12-yaml-membership
  |         |               |           |                +---> 01-14-in-memory-streaming --+
  |         +---------------+-----------+                                                  |
  |         |                                                                              |
  +---> 01-03-errors -----> 01-04-serialization                                            |
                                |                                                          |
                                +---> 01-06-grain-decorator                                |
                                |         |                                                |
                                |     01-07-grain-base-class                               |
                                |         |                                                |
                                +---------+---> 01-08-grain-runtime --+                    |
                                                     |                |                    |
                                                     +---> 01-09-grain-reference           |
                                                     |         |                           |
                                                     +---> 01-13-grain-timers              |
                                                     |                                     |
                                                     +---> 01-10-di-container              |
                                                              |                            |
                                                              +---+---+---+---+------------+
                                                                  |
                                                         01-15-silo
                                                                  |
                                                         01-16-counter-grain
                                                                  |
                                                         01-17-counter-app
                                                                  |
                                                         01-18-counter-client
```

## Phase 1 Tasks

### Layer 1: Foundation (no dependencies between these)

| # | Task | Status | Dependencies |
|---|---|---|---|
| 01-01 | [Project Setup](task-01-01-project-setup.md) | [x] | None |

### Layer 2: Core Types (depends on 01-01)

| # | Task | Status | Dependencies |
|---|---|---|---|
| 01-02 | [Core Identity Types](task-01-02-core-types.md) | [x] | 01-01 |
| 01-03 | [Error Types](task-01-03-errors.md) | [x] | 01-01 |

### Layer 3: Serialization and ABCs (depends on 01-02, 01-03)

| # | Task | Status | Dependencies |
|---|---|---|---|
| 01-04 | [Serialization](task-01-04-serialization.md) | [x] | 01-01, 01-03 |
| 01-05 | [Provider ABCs](task-01-05-provider-abcs.md) | [x] | 01-02, 01-03 |

### Layer 4: Grain System (depends on 01-04, 01-05)

| # | Task | Status | Dependencies |
|---|---|---|---|
| 01-06 | [Grain Decorator](task-01-06-grain-decorator.md) | [x] | 01-02, 01-04 |
| 01-07 | [Grain Base Class](task-01-07-grain-base-class.md) | [x] | 01-02, 01-03, 01-06 |
| 01-11 | [File Storage Provider](task-01-11-file-storage.md) | [x] | 01-04, 01-05 |
| 01-12 | [YAML Membership Provider](task-01-12-yaml-membership.md) | [x] | 01-02, 01-05 |
| 01-14 | [In-Memory Stream Provider](task-01-14-in-memory-streaming.md) | [x] | 01-05, 01-08 |

### Layer 5: Runtime (depends on Layer 4)

| # | Task | Status | Dependencies |
|---|---|---|---|
| 01-08 | [Grain Runtime](task-01-08-grain-runtime.md) | [x] | 01-03, 01-04, 01-05, 01-06, 01-07 |

### Layer 6: Runtime Consumers (depends on 01-08)

| # | Task | Status | Dependencies |
|---|---|---|---|
| 01-09 | [Grain Reference](task-01-09-grain-reference.md) | [x] | 01-02, 01-04, 01-08 |
| 01-10 | [DI Container](task-01-10-di-container.md) | [x] | 01-05, 01-08, 01-09 |
| 01-13 | [Grain Timers](task-01-13-grain-timers.md) | [x] | 01-08 |

### Layer 7: Silo Assembly (depends on all above)

| # | Task | Status | Dependencies |
|---|---|---|---|
| 01-15 | [Silo](task-01-15-silo.md) | [x] | 01-08, 01-09, 01-10, 01-11, 01-12, 01-13, 01-14 |

### Layer 8: Sample Application

| # | Task | Status | Dependencies |
|---|---|---|---|
| 01-16 | [Counter Grain](task-01-16-counter-grain.md) | [x] | 01-06, 01-07, 01-15 |
| 01-17 | [Counter App (Standalone Silo)](task-01-17-counter-app.md) | [x] | 01-11, 01-12, 01-15, 01-16 |
| 01-18 | [Counter Client (Gateway Protocol)](task-01-18-counter-client.md) | [x] | 01-17 |

## Phase 2 Dependency Graph

```
                                   +--> 02-02-hash-ring ----+----------+
                                   |                         |          |
 02-01-cluster-identity --+--------+--> 02-03-placement -----+          |
                          |                                   |          |
                          |   +--> 02-04-transport-abcs --+---+          |
                          |   |                            |             |
                          +---+                            +--> 02-05-wire-protocol --+
                          |                                                            |
                          |                                    02-06-silo-connection --+
                          |                                              |
                          |                                    02-07-connection-manager
                          |                                              |
                          |                                    02-08-tcp-cluster-transport ---+
                          |                                                                    |
                          +--> 02-09-membership-extensions --+                                 |
                                       |                     |                                 |
                                       +--> 02-10-file-locking                                 |
                                       |                                                       |
                                       +--> 02-11-failure-detector <---------------------------+
                                                        |                                      |
                                            02-12-grain-directory-port                         |
                                                        |                                      |
                                            02-13-distributed-directory <----------------------+
                                                        |
                                            +-----------+-----------+
                                            |           |           |
                                   02-14-directory-cache  02-15-directory-recovery
                                            |           |
                                            +-----+-----+
                                                  |
                                      02-16-remote-grain-invoke
                                                  |
                                      02-17-silo-lifecycle-stages
                                                  |
                                      02-18-multi-silo-integration-tests
                                                  |
                                      02-19-counter-sample-multi-silo

 02-09-membership-extensions --+--> 02-20-postgresql-membership
                                |
 01-05-provider-abcs ----------+--> 02-21-postgresql-storage
```

## Phase 2 Tasks

### Layer 9: Phase 2 Foundation (can run in parallel after Phase 1)

| # | Task | Status | Dependencies |
|---|---|---|---|
| 02-01 | [Cluster Identity](task-02-01-cluster-identity.md) | [ ] | 01-02 |
| 02-02 | [Consistent Hash Ring](task-02-02-consistent-hash-ring.md) | [ ] | 02-01 |
| 02-03 | [Placement Strategies](task-02-03-placement-strategies.md) | [ ] | 02-01 |

### Layer 10: Transport -- Pluggable Inter-Silo Communication

| # | Task | Status | Dependencies |
|---|---|---|---|
| 02-04 | [Transport ABCs](task-02-04-transport-abcs.md) | [ ] | 02-01 |
| 02-05 | [Wire Protocol](task-02-05-wire-protocol.md) | [ ] | 02-01, 02-04 |
| 02-06 | [Silo Connection](task-02-06-silo-connection.md) | [ ] | 02-04, 02-05 |
| 02-07 | [Silo Connection Manager](task-02-07-silo-connection-manager.md) | [ ] | 02-05, 02-06 |
| 02-08 | [TCP Cluster Transport](task-02-08-tcp-cluster-transport.md) | [ ] | 02-04, 02-07 |

### Layer 11: Membership Protocol

| # | Task | Status | Dependencies |
|---|---|---|---|
| 02-09 | [Membership Table Extensions](task-02-09-membership-table-extensions.md) | [ ] | 02-01, 01-12 |
| 02-10 | [File Locking for Membership](task-02-10-file-locking-membership.md) | [ ] | 02-09 |
| 02-11 | [Failure Detector](task-02-11-failure-detector.md) | [ ] | 02-02, 02-08, 02-09 |

### Layer 12: Distributed Grain Directory

| # | Task | Status | Dependencies |
|---|---|---|---|
| 02-12 | [Grain Directory Port](task-02-12-grain-directory-port.md) | [ ] | 01-08 |
| 02-13 | [Distributed Grain Directory](task-02-13-distributed-grain-directory.md) | [ ] | 02-02, 02-03, 02-08, 02-12 |
| 02-14 | [Directory Cache](task-02-14-directory-cache.md) | [ ] | 02-13 |
| 02-15 | [Directory Recovery](task-02-15-directory-recovery.md) | [ ] | 02-11, 02-13 |

### Layer 13: Runtime Integration

| # | Task | Status | Dependencies |
|---|---|---|---|
| 02-16 | [Remote Grain Invocation](task-02-16-remote-grain-invoke.md) | [ ] | 02-08, 02-13, 02-14 |
| 02-17 | [Silo Lifecycle Stages](task-02-17-silo-lifecycle-stages.md) | [ ] | 02-08, 02-09, 02-11, 02-13, 02-14 |

### Layer 14: Phase 2 Validation

| # | Task | Status | Dependencies |
|---|---|---|---|
| 02-18 | [Multi-Silo Integration Tests](task-02-18-multi-silo-integration-tests.md) | [ ] | 02-16, 02-17 |
| 02-19 | [Counter Sample Multi-Silo](task-02-19-counter-sample-multi-silo.md) | [ ] | 01-17, 01-18, 02-16, 02-17, 02-18 |

### Production Backends (PostgreSQL)

Production-grade backing store for Phase 2. Both providers share a single PostgreSQL instance so production deployments need only one stateful dependency. Can run in parallel with the rest of Phase 2 once their single upstream is done.

| # | Task | Status | Dependencies |
|---|---|---|---|
| 02-20 | [PostgreSQL Membership Provider](task-02-20-postgresql-membership.md) | [ ] | 02-09 |
| 02-21 | [PostgreSQL Storage Provider](task-02-21-postgresql-storage.md) | [ ] | 01-05, 01-11 |

## Parallel Execution Opportunities

Tasks within the same layer can be implemented in parallel:

- **Phase 1 Layer 2**: 01-02, 01-03 (parallel)
- **Phase 1 Layer 3**: 01-04, 01-05 (parallel)
- **Phase 1 Layer 4**: 01-06, 01-07, 01-11, 01-12 (parallel; 01-14 waits for 01-08)
- **Phase 1 Layer 6**: 01-09, 01-10, 01-13 (parallel after 01-08)
- **Phase 2 Layer 9**: 02-02, 02-03 (parallel after 02-01)
- **Phase 2 Layer 10**: 02-06, 02-07 sequential; 02-04 can start alongside 02-01
- **Phase 2 Layer 11**: 02-10 parallel with 02-11 (02-10 only blocks on 02-09); 02-11 waits on 02-02 and 02-08
- **Phase 2 Layer 12**: 02-14, 02-15 parallel after 02-13
- **Phase 2 Layer 13**: 02-16, 02-17 mostly independent; schedule together
- **Production Backends**: 02-20 and 02-21 parallel with everything else in Phase 2; 02-20 only blocks on 02-09, 02-21 only blocks on 01-05 and 01-11
