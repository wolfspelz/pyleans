"""Grain placement strategies.

When the grain directory discovers that a grain has no activation anywhere
in the cluster, it asks a :class:`PlacementStrategy` to pick a silo to host
the new activation. Placement is a pure decision layer — it takes
``(grain_id, caller, active_silos)`` and returns a silo. It has no
knowledge of the directory, the transport, or the network; downstream
components inject the concrete strategy, so unit tests can exercise each
path in isolation.

See :doc:`../../../docs/adr/adr-single-activation-cluster` for the role
placement plays in the single-activation contract: placement decides
*where* a new activation lands; the directory enforces that exactly one
such decision ever commits per :class:`GrainId`.
"""

from __future__ import annotations

import logging
import random
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import TypeVar

from pyleans.cluster.identity import hash_grain_id
from pyleans.identity import GrainId, SiloAddress

_log = logging.getLogger(__name__)

DEFAULT_PLACEMENT_ATTR = "_placement_strategy"

TGrain = TypeVar("TGrain", bound=type)


class NoSilosAvailableError(RuntimeError):
    """Raised when placement is asked to pick from an empty silo list.

    The directory (task-02-13) catches this and surfaces it to the caller
    as a ``ClusterNotReadyError`` — distinguishing "no silos" (configuration
    / startup) from "all silos unreachable" (network) helps operations.
    """


class PlacementStrategy(ABC):
    """Decides which silo a new grain activation lands on."""

    @abstractmethod
    def pick_silo(
        self,
        grain_id: GrainId,
        caller: SiloAddress | None,
        active_silos: list[SiloAddress],
    ) -> SiloAddress:
        """Return the silo that should host a new activation of ``grain_id``.

        Raises :class:`NoSilosAvailableError` if ``active_silos`` is empty.
        ``caller`` is the silo making the request (``None`` for external
        clients, which have no local silo).
        """


class RandomPlacement(PlacementStrategy):
    """Uniform random selection from the active silos.

    The first-attempt pick is deterministic — seeded from
    ``hash_grain_id(grain_id)`` — so two clients racing to activate the
    same missing grain land on the same silo and cleanly de-duplicate at
    the directory owner. Retries after the first attempt pick
    independently so a single failing silo is not hammered repeatedly.
    """

    def __init__(self) -> None:
        self._retry_rng = random.Random()

    def pick_silo(
        self,
        grain_id: GrainId,
        caller: SiloAddress | None,
        active_silos: list[SiloAddress],
    ) -> SiloAddress:
        _require_silos(grain_id, active_silos)
        return self._first_attempt_pick(grain_id, active_silos)

    def pick_silo_retry(
        self,
        grain_id: GrainId,
        active_silos: list[SiloAddress],
    ) -> SiloAddress:
        """Pick a silo on a retry after a prior attempt failed.

        Uses an unseeded RNG so two retrying callers do not pick the same
        silo. Callers that want a strictly random pick (no seed) can use
        this directly.
        """
        _require_silos(grain_id, active_silos)
        pick = self._retry_rng.choice(active_silos)
        _log.debug("random placement retry: grain=%s picked=%s", grain_id, pick.silo_id)
        return pick

    @staticmethod
    def _first_attempt_pick(grain_id: GrainId, active_silos: list[SiloAddress]) -> SiloAddress:
        seeded = random.Random(hash_grain_id(grain_id))
        sorted_silos = sorted(active_silos, key=lambda s: s.silo_id)
        pick = seeded.choice(sorted_silos)
        _log.debug("random placement first attempt: grain=%s picked=%s", grain_id, pick.silo_id)
        return pick


class PreferLocalPlacement(PlacementStrategy):
    """Place the activation on ``caller`` if valid; otherwise pick randomly.

    This keeps chattiness local when a grain on silo A activates another
    grain — the new activation stays on A so the two can talk without a
    network hop. When the call originates externally (``caller is None``)
    or ``caller`` is no longer in the active set, the strategy falls back
    to :class:`RandomPlacement`.
    """

    def __init__(self) -> None:
        self._fallback = RandomPlacement()

    def pick_silo(
        self,
        grain_id: GrainId,
        caller: SiloAddress | None,
        active_silos: list[SiloAddress],
    ) -> SiloAddress:
        _require_silos(grain_id, active_silos)
        if caller is not None and caller in active_silos:
            _log.debug(
                "prefer-local placement: grain=%s keeping on caller=%s",
                grain_id,
                caller.silo_id,
            )
            return caller
        return self._fallback.pick_silo(grain_id, caller, active_silos)


def placement(strategy: type[PlacementStrategy]) -> Callable[[TGrain], TGrain]:
    """Attach a placement strategy class to a grain class.

    The directory calls ``grain_class._placement_strategy.pick_silo(...)``
    (instantiating lazily) when resolving a missing activation. Grain
    classes without an explicit ``@placement`` decorator inherit
    :class:`RandomPlacement` via :func:`get_placement_strategy`.
    """
    if not isinstance(strategy, type) or not issubclass(strategy, PlacementStrategy):
        raise TypeError(f"placement() requires a PlacementStrategy subclass, got {strategy!r}")

    def decorator(cls: TGrain) -> TGrain:
        type.__setattr__(cls, DEFAULT_PLACEMENT_ATTR, strategy)
        return cls

    return decorator


def get_placement_strategy(grain_class: type) -> PlacementStrategy:
    """Return the placement-strategy instance for a grain class.

    Falls back to :class:`RandomPlacement` for grain classes without an
    explicit ``@placement`` decoration.
    """
    strategy_class = getattr(grain_class, DEFAULT_PLACEMENT_ATTR, None)
    if strategy_class is None:
        return RandomPlacement()
    return strategy_class()  # type: ignore[no-any-return]


def _require_silos(grain_id: GrainId, active_silos: list[SiloAddress]) -> None:
    if not active_silos:
        raise NoSilosAvailableError(f"cannot place grain {grain_id}: no active silos")
