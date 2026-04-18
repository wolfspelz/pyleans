# Task 12: YAML File Membership Provider

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly — clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-05-provider-abcs.md](task-05-provider-abcs.md)
- [task-02-core-types.md](task-02-core-types.md)

## References
- [adr-provider-interfaces](../adr/adr-provider-interfaces.md)
- [orleans-cluster.md](../orleans-architecture/orleans-cluster.md) -- Membership Protocol, Membership Table

## Description

Implement a YAML-file-based membership provider. A single YAML file acts as the
membership table. Human-readable so operators can inspect cluster state.

### Files to create
- `src/pyleans/pyleans/server/providers/yaml_membership.py`

### Design

```python
class YamlMembershipProvider(MembershipProvider):
    """
    Stores membership in a single YAML file.

    File format:
        version: 3
        silos:
          - id: "192.168.1.10_11111_1713180000"
            host: "192.168.1.10"
            port: 11111
            epoch: 1713180000
            status: "active"
            last_heartbeat: "2026-04-15T12:00:00Z"
            start_time: "2026-04-15T11:55:00Z"
          - id: "192.168.1.10_11112_1713180005"
            host: "192.168.1.10"
            port: 11112
            epoch: 1713180005
            status: "active"
            last_heartbeat: "2026-04-15T12:00:01Z"
            start_time: "2026-04-15T11:55:05Z"
    """

    def __init__(self, file_path: str = "./data/membership.yaml"):
        self._file_path = Path(file_path)

    async def register_silo(self, silo: SiloInfo) -> None:
        """Add or update a silo entry."""

    async def unregister_silo(self, silo_id: str) -> None:
        """Remove a silo entry."""

    async def get_active_silos(self) -> list[SiloInfo]:
        """Return silos with status 'active'."""

    async def heartbeat(self, silo_id: str) -> None:
        """Update last_heartbeat timestamp."""

    async def update_status(self, silo_id: str, status: SiloStatus) -> None:
        """Change silo status (e.g. active -> shutting_down)."""
```

### File locking

Multiple silo processes may read/write the same YAML file. Use a simple
file-lock approach:
- Write to a temp file, then atomic rename (on POSIX)
- On Windows, use a `.lock` file with retry

For the PoC, a simple read-modify-write with a `.lock` file is sufficient.

### Version counter

The `version` field increments on every write. This allows silos to detect
changes by polling (compare version numbers). Matches Orleans' membership
table version row.

### Acceptance criteria

- [x] YAML file created on first `register_silo`
- [x] Multiple silos can be registered
- [x] `get_active_silos` filters by status
- [x] `heartbeat` updates the timestamp
- [x] `unregister_silo` removes the entry
- [x] `update_status` changes status field
- [x] Version increments on every write
- [x] File is valid, human-readable YAML
- [x] Unit tests with temp directory

**Deferred to Phase 2**: Concurrent access via file locking (not needed for single-silo).

## Findings of code review
_To be filled when task is complete._

## Findings of security review
_To be filled when task is complete._

## Summary of implementation

### Files created
- `src/pyleans/pyleans/server/providers/yaml_membership.py` — YamlMembershipProvider
- `src/pyleans/test/test_yaml_membership.py` — 14 tests

### Key decisions
- Single YAML file with version counter that increments on every write.
- Silo ID is `SiloAddress.encoded` (host_port_epoch).
- `register_silo` updates in-place if silo already exists.
- `heartbeat` and `update_status` raise MembershipError for unknown silos.

### Deviations
- No file locking in PoC (simple read-modify-write). Sufficient for single-silo dev mode.

### Test coverage
- 14 tests: register (create file, get, multiple, update), unregister, get_active_silos (filter, empty), heartbeat (update, unknown), update_status, version increment, YAML readability.