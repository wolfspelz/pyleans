# Task 11: YAML File Membership Provider

> **Coding rules**: Follow [CLAUDE.md](../CLAUDE.md) strictly — clean code, SOLID, strict type hints, mandatory tests.\
> **On completion**: Fill in "Summary of implementation" at the bottom with files created, decisions made, deviations, and test coverage.

## Dependencies
- [task-05-provider-abcs.md](task-05-provider-abcs.md)
- [task-02-core-types.md](task-02-core-types.md)

## References
- [pyleans-plan.md](../docs/pyleans-plan.md) -- Decision 5
- [orleans-cluster.md](../docs/orleans-cluster.md) -- Membership Protocol, Membership Table

## Description

Implement a YAML-file-based membership provider. A single YAML file acts as the
membership table. Human-readable so operators can inspect cluster state.

### Files to create
- `src/pyleans/server/providers/yaml_membership.py`

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

    def __init__(self, file_path: str = "./pyleans-data/membership.yaml"):
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

- [ ] YAML file created on first `register_silo`
- [ ] Multiple silos can be registered
- [ ] `get_active_silos` filters by status
- [ ] `heartbeat` updates the timestamp
- [ ] `unregister_silo` removes the entry
- [ ] `update_status` changes status field
- [ ] Version increments on every write
- [ ] File is valid, human-readable YAML
- [ ] Concurrent access handled via file locking
- [ ] Unit tests with temp directory

## Summary of implementation
_To be filled when task is complete._