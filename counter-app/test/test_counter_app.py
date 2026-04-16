"""Integration tests for the counter-app standalone silo.

These tests use real FileStorageProvider and YamlMembershipProvider
with temporary directories to verify end-to-end behavior.
"""

from pathlib import Path

import yaml
from counter_app.counter_grain import CounterGrain
from pyleans.identity import GrainId
from pyleans.server.providers.file_storage import FileStorageProvider
from pyleans.server.providers.memory_stream import InMemoryStreamProvider
from pyleans.server.providers.yaml_membership import YamlMembershipProvider
from pyleans.server.silo import Silo


def make_silo(tmp_path: Path) -> Silo:
    """Create a silo wired to real file-based providers in a temp directory."""
    return Silo(
        grains=[CounterGrain],
        storage_providers={
            "default": FileStorageProvider(str(tmp_path / "storage")),
        },
        membership_provider=YamlMembershipProvider(
            str(tmp_path / "membership.yaml"),
        ),
        stream_providers={"default": InMemoryStreamProvider()},
        gateway_port=0,
    )


class TestSiloStartStop:
    """Verify the silo starts, registers in membership, and shuts down cleanly."""

    async def test_membership_file_created_on_start(self, tmp_path: Path) -> None:
        silo = make_silo(tmp_path)
        await silo.start_background()

        membership_path = tmp_path / "membership.yaml"
        assert membership_path.exists()

        data = yaml.safe_load(membership_path.read_text(encoding="utf-8"))
        assert len(data["silos"]) == 1
        assert data["silos"][0]["status"] == "active"

        await silo.stop()

    async def test_membership_empty_after_clean_shutdown(self, tmp_path: Path) -> None:
        silo = make_silo(tmp_path)
        await silo.start_background()
        await silo.stop()

        membership_path = tmp_path / "membership.yaml"
        data = yaml.safe_load(membership_path.read_text(encoding="utf-8"))
        assert data["silos"] == []

    async def test_stop_is_idempotent(self, tmp_path: Path) -> None:
        silo = make_silo(tmp_path)
        await silo.start_background()
        await silo.stop()
        await silo.stop()  # second stop should be a no-op


class TestGrainStatePersistence:
    """Verify grain state is persisted to files and survives silo restarts."""

    async def test_state_file_created_on_grain_call(self, tmp_path: Path) -> None:
        silo = make_silo(tmp_path)
        await silo.start_background()

        ref = silo.grain_factory.get_grain(CounterGrain, "test-counter")
        await ref.increment()

        storage_dir = tmp_path / "storage" / "CounterGrain"
        assert storage_dir.exists()
        state_files = list(storage_dir.glob("*.json"))
        assert len(state_files) == 1
        assert state_files[0].name == "test-counter.json"

        await silo.stop()

    async def test_state_survives_silo_restart(self, tmp_path: Path) -> None:
        silo1 = make_silo(tmp_path)
        await silo1.start_background()

        ref = silo1.grain_factory.get_grain(CounterGrain, "persist")
        await ref.increment()
        await ref.increment()
        await ref.increment()
        assert await ref.get_value() == 3

        await silo1.stop()

        silo2 = make_silo(tmp_path)
        await silo2.start_background()

        ref2 = silo2.grain_factory.get_grain(CounterGrain, "persist")
        assert await ref2.get_value() == 3

        await silo2.stop()

    async def test_multiple_grains_create_separate_files(self, tmp_path: Path) -> None:
        silo = make_silo(tmp_path)
        await silo.start_background()

        for name in ["alpha", "beta", "gamma"]:
            ref = silo.grain_factory.get_grain(CounterGrain, name)
            await ref.increment()

        storage_dir = tmp_path / "storage" / "CounterGrain"
        state_files = sorted(f.stem for f in storage_dir.glob("*.json"))
        assert state_files == ["alpha", "beta", "gamma"]

        await silo.stop()


class TestGrainLifecycle:
    """Verify grain deactivation saves state and silo shutdown deactivates all."""

    async def test_deactivate_saves_state_to_file(self, tmp_path: Path) -> None:
        silo = make_silo(tmp_path)
        await silo.start_background()

        ref = silo.grain_factory.get_grain(CounterGrain, "deact")
        await ref.set_value(42)

        gid = GrainId("CounterGrain", "deact")
        await silo.runtime.deactivate_grain(gid)

        # Re-activate and verify state was persisted
        ref2 = silo.grain_factory.get_grain(CounterGrain, "deact")
        assert await ref2.get_value() == 42

        await silo.stop()

    async def test_shutdown_deactivates_all_grains(self, tmp_path: Path) -> None:
        silo = make_silo(tmp_path)
        await silo.start_background()

        for i in range(3):
            ref = silo.grain_factory.get_grain(CounterGrain, f"g{i}")
            await ref.set_value(i * 10)

        await silo.stop()

        # All activations should be cleared
        assert len(silo.runtime.activations) == 0

        # State should be on disk — verify by restarting
        silo2 = make_silo(tmp_path)
        await silo2.start_background()

        for i in range(3):
            ref = silo2.grain_factory.get_grain(CounterGrain, f"g{i}")
            assert await ref.get_value() == i * 10

        await silo2.stop()


class TestFullIntegration:
    """End-to-end: start silo, call grain, stop silo, verify persistence."""

    async def test_full_lifecycle(self, tmp_path: Path) -> None:
        membership_path = tmp_path / "membership.yaml"

        # Start silo
        silo = make_silo(tmp_path)
        await silo.start_background()
        assert silo.started

        # Verify membership while running
        data = yaml.safe_load(membership_path.read_text(encoding="utf-8"))
        assert len(data["silos"]) == 1
        assert data["silos"][0]["status"] == "active"

        # Use grains
        c1 = silo.grain_factory.get_grain(CounterGrain, "counter-1")
        c2 = silo.grain_factory.get_grain(CounterGrain, "counter-2")

        await c1.increment()
        await c1.increment()
        await c2.set_value(100)

        assert await c1.get_value() == 2
        assert await c2.get_value() == 100

        # Stop silo
        await silo.stop()
        assert not silo.started

        # Verify membership cleared after shutdown
        data = yaml.safe_load(membership_path.read_text(encoding="utf-8"))
        assert data["silos"] == []

        # Verify state files exist
        storage_dir = tmp_path / "storage" / "CounterGrain"
        assert (storage_dir / "counter-1.json").exists()
        assert (storage_dir / "counter-2.json").exists()

        # Restart and verify state survived
        silo2 = make_silo(tmp_path)
        await silo2.start_background()

        c1r = silo2.grain_factory.get_grain(CounterGrain, "counter-1")
        c2r = silo2.grain_factory.get_grain(CounterGrain, "counter-2")
        assert await c1r.get_value() == 2
        assert await c2r.get_value() == 100

        await silo2.stop()
