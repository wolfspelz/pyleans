"""Exception hierarchy for pyleans."""


class PyleansError(Exception):
    """Base class for all pyleans errors."""


class GrainError(PyleansError):
    """Error related to grain operations."""


class GrainNotFoundError(GrainError):
    """Grain type not registered with the silo."""


class GrainActivationError(GrainError):
    """Failed to activate a grain."""


class GrainDeactivationError(GrainError):
    """Failed to deactivate a grain."""


class GrainMethodError(GrainError):
    """Error invoking a grain method."""


class StorageError(PyleansError):
    """Error in a storage provider."""


class StorageInconsistencyError(StorageError):
    """ETag mismatch — optimistic concurrency violation."""

    def __init__(self, expected_etag: str | None, actual_etag: str | None) -> None:
        self.expected_etag = expected_etag
        self.actual_etag = actual_etag
        super().__init__(
            f"ETag mismatch: expected {expected_etag!r}, got {actual_etag!r}"
        )


class MembershipError(PyleansError):
    """Error in the membership provider."""


class TransportError(PyleansError):
    """Error in the transport layer."""


class SerializationError(PyleansError):
    """Error serializing or deserializing data."""
