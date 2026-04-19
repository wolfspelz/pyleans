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


class GrainCallError(GrainError):
    """Remote grain-call envelope failed before the method executed.

    Covers transport / encoding / deadline-expired failures — distinct
    from exceptions raised *inside* the grain method, which surface
    as :class:`RemoteGrainException`.
    """


class RemoteGrainException(GrainError):  # noqa: N818  (name fixed by task-02-16 spec)
    """An exception raised by a grain running on another silo.

    The remote exception's class is NOT reconstructed on the caller side
    (security-first choice — avoids pickle-style code-execution risk and
    version skew of exception classes). The original type name,
    message, and stringified traceback are preserved verbatim so
    callers can match on ``exception_type`` and operators see the
    remote stack in logs.
    """

    def __init__(
        self,
        exception_type: str,
        message: str,
        remote_traceback: str | None = None,
    ) -> None:
        self.exception_type = exception_type
        self.message = message
        self.remote_traceback = remote_traceback
        super().__init__(f"{exception_type}: {message}")


class StorageError(PyleansError):
    """Error in a storage provider."""


class StorageInconsistencyError(StorageError):
    """ETag mismatch — optimistic concurrency violation."""

    def __init__(self, expected_etag: str | None, actual_etag: str | None) -> None:
        self.expected_etag = expected_etag
        self.actual_etag = actual_etag
        super().__init__(f"ETag mismatch: expected {expected_etag!r}, got {actual_etag!r}")


class StorageUnavailableError(StorageError):
    """Storage backend is unreachable.

    The grain runtime treats this as an infrastructure fault (log and
    continue with cached state if possible); distinct from
    :class:`StorageInconsistencyError` which is a correctness concern
    the grain author may need to handle.
    """


class StorageSerializationError(StorageError):
    """State could not be serialised / deserialised to the storage backend."""


class MembershipError(PyleansError):
    """Error in the membership provider."""


class TransportError(PyleansError):
    """Error in the transport layer."""


class SerializationError(PyleansError):
    """Error serializing or deserializing data."""
