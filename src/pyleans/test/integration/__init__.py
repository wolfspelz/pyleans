"""End-to-end multi-silo integration tests.

These tests assemble several in-process "silos" from the Phase 2
building blocks (transport + directory + cache + runtime) and
exercise the contract-level behaviours that only emerge from
multi-silo interaction: cluster formation, owner-routed grain
calls, cache hits skipping the owner RPC, and single-activation
under contention.

Marked ``@pytest.mark.integration`` so the fast unit suite stays
fast; run explicitly with ``pytest -m integration``.
"""
