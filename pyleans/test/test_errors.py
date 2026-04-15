"""Tests for pyleans.errors — exception hierarchy."""

import pytest

from pyleans.errors import (
    GrainActivationError,
    GrainDeactivationError,
    GrainError,
    GrainMethodError,
    GrainNotFoundError,
    MembershipError,
    PyleansError,
    SerializationError,
    StorageError,
    StorageInconsistencyError,
    TransportError,
)


class TestExceptionHierarchy:
    """All exceptions must inherit from PyleansError."""

    @pytest.mark.parametrize(
        "exc_cls",
        [
            GrainError,
            GrainNotFoundError,
            GrainActivationError,
            GrainDeactivationError,
            GrainMethodError,
            StorageError,
            StorageInconsistencyError,
            MembershipError,
            TransportError,
            SerializationError,
        ],
    )
    def test_inherits_from_pyleans_error(self, exc_cls: type[PyleansError]) -> None:
        assert issubclass(exc_cls, PyleansError)

    def test_grain_errors_inherit_from_grain_error(self) -> None:
        for cls in [
            GrainNotFoundError,
            GrainActivationError,
            GrainDeactivationError,
            GrainMethodError,
        ]:
            assert issubclass(cls, GrainError)

    def test_storage_inconsistency_inherits_from_storage_error(self) -> None:
        assert issubclass(StorageInconsistencyError, StorageError)


class TestPyleansError:
    def test_message(self) -> None:
        err = PyleansError("something broke")
        assert str(err) == "something broke"

    def test_catchable_as_exception(self) -> None:
        with pytest.raises(Exception):
            raise PyleansError("test")


class TestStorageInconsistencyError:
    def test_carries_etags(self) -> None:
        err = StorageInconsistencyError(expected_etag="abc", actual_etag="def")
        assert err.expected_etag == "abc"
        assert err.actual_etag == "def"

    def test_message_format(self) -> None:
        err = StorageInconsistencyError(expected_etag="abc", actual_etag="def")
        assert "abc" in str(err)
        assert "def" in str(err)

    def test_none_etags(self) -> None:
        err = StorageInconsistencyError(expected_etag=None, actual_etag="def")
        assert err.expected_etag is None
        assert err.actual_etag == "def"

    def test_both_none_etags(self) -> None:
        err = StorageInconsistencyError(expected_etag=None, actual_etag=None)
        assert err.expected_etag is None
        assert err.actual_etag is None

    def test_catchable_as_storage_error(self) -> None:
        with pytest.raises(StorageError):
            raise StorageInconsistencyError(expected_etag="a", actual_etag="b")

    def test_catchable_as_pyleans_error(self) -> None:
        with pytest.raises(PyleansError):
            raise StorageInconsistencyError(expected_etag="a", actual_etag="b")


class TestGrainErrors:
    def test_grain_not_found_message(self) -> None:
        err = GrainNotFoundError("CounterGrain not registered")
        assert "CounterGrain" in str(err)

    def test_grain_activation_error(self) -> None:
        err = GrainActivationError("failed to load state")
        assert "failed to load state" in str(err)

    def test_grain_method_error(self) -> None:
        err = GrainMethodError("method 'foo' raised ValueError")
        assert "foo" in str(err)
