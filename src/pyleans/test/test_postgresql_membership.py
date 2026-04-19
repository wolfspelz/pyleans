"""Unit tests for :class:`PostgreSQLMembershipProvider`.

The PostgreSQL extra (``asyncpg``) is not available in the default
dev environment. These tests validate the row-coercion / timestamp
helpers / construction validation without a live database. Full
integration tests against a containerized Postgres live behind
``@pytest.mark.integration`` as task 02-18 extends the harness.
"""

from __future__ import annotations

import datetime
from types import SimpleNamespace

import pytest
from pyleans.identity import SiloStatus
from pyleans.server.providers.postgresql_membership import (
    PostgreSQLMembershipProvider,
    _load_asyncpg,
    _ts,
    _with_etag,
)


class TestConstruction:
    def test_rejects_inverted_pool_sizes(self) -> None:
        # Act / Assert
        with pytest.raises(ValueError, match="max_pool_size"):
            PostgreSQLMembershipProvider(
                dsn="postgres://", cluster_id="dev", min_pool_size=5, max_pool_size=2
            )

    def test_accepts_matching_pool_sizes(self) -> None:
        # Act (must not raise)
        provider = PostgreSQLMembershipProvider(
            dsn="postgres://", cluster_id="dev", min_pool_size=4, max_pool_size=4
        )

        # Assert
        assert provider.cluster_id == "dev"


class TestMissingAsyncpg:
    def test_load_asyncpg_raises_clear_error_when_missing(self) -> None:
        # Arrange - asyncpg is genuinely not installed in this environment
        import sys

        saved = sys.modules.pop("asyncpg", None)
        try:
            # Act / Assert
            with pytest.raises(ImportError, match="postgresql"):
                _load_asyncpg()
        finally:
            if saved is not None:
                sys.modules["asyncpg"] = saved


class TestTimestampHelper:
    def test_ts_converts_unix_epoch_to_tz_aware_datetime(self) -> None:
        # Arrange
        now = 1_700_000_000.5

        # Act
        dt = _ts(now)

        # Assert
        assert dt.tzinfo is not None
        assert dt.tzinfo.utcoffset(dt) == datetime.timedelta(0)
        assert dt.timestamp() == pytest.approx(now)


class TestWithEtag:
    def test_preserves_fields_and_updates_etag(self) -> None:
        # Arrange
        from pyleans.identity import SiloAddress, SiloInfo

        silo = SiloInfo(
            address=SiloAddress(host="a", port=11111, epoch=1),
            status=SiloStatus.ACTIVE,
            last_heartbeat=1.0,
            start_time=1.0,
            cluster_id="dev",
        )

        # Act
        updated = _with_etag(silo, "42")

        # Assert
        assert updated.etag == "42"
        assert updated.address == silo.address
        assert updated.status == silo.status
        assert updated.cluster_id == silo.cluster_id


class TestRowCoercion:
    def test_row_to_silo_shape(self) -> None:
        # Arrange - use a SimpleNamespace + dict-like for minimal fake
        provider = PostgreSQLMembershipProvider(dsn="postgres://", cluster_id="dev")
        row = {
            "host": "host-a",
            "port": 11111,
            "epoch": 7,
            "status": "active",
            "gateway_port": 30000,
            "start_time": _ts(1000.0),
            "last_heartbeat": _ts(1010.0),
            "i_am_alive": _ts(1010.0),
            "suspicions": [{"suspecting_silo": "b:11111:1", "timestamp": 1005.0}],
            "cluster_id": "dev",
            "etag_version": 3,
        }

        # Act
        silo = provider._row_to_silo(row)

        # Assert
        assert silo.address.host == "host-a"
        assert silo.address.port == 11111
        assert silo.status == SiloStatus.ACTIVE
        assert silo.gateway_port == 30000
        assert silo.last_heartbeat == pytest.approx(1010.0)
        assert silo.etag == "3"
        assert len(silo.suspicions) == 1
        assert silo.suspicions[0].suspecting_silo == "b:11111:1"

    def test_row_to_silo_accepts_jsonb_string(self) -> None:
        # Arrange - asyncpg may return JSONB as a raw JSON string
        provider = PostgreSQLMembershipProvider(dsn="postgres://", cluster_id="dev")
        row = {
            "host": "h",
            "port": 1,
            "epoch": 1,
            "status": "active",
            "gateway_port": None,
            "start_time": _ts(0.0),
            "last_heartbeat": _ts(0.0),
            "i_am_alive": _ts(0.0),
            "suspicions": '[{"suspecting_silo": "x:1:1", "timestamp": 3.0}]',
            "cluster_id": "dev",
            "etag_version": 1,
        }

        # Act
        silo = provider._row_to_silo(row)

        # Assert
        assert silo.suspicions[0].suspecting_silo == "x:1:1"
        assert silo.suspicions[0].timestamp == pytest.approx(3.0)


# Imported helper reference to keep ruff happy in this test module.
_ = SimpleNamespace
