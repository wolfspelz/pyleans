"""Dependency injection container for the pyleans framework."""

import logging

from dependency_injector import containers, providers

from pyleans.reference import GrainFactory
from pyleans.serialization import JsonSerializer, Serializer
from pyleans.server.runtime import GrainRuntime
from pyleans.server.timer import TimerRegistry


class PyleansContainer(containers.DeclarativeContainer):
    """Base container with framework services.

    Users extend this with their own services:

        class AppContainer(PyleansContainer):
            email = providers.Singleton(SmtpEmailService)

    The runtime requires storage_providers and serializer. These are
    provided with sensible defaults that can be overridden.
    """

    config = providers.Configuration()

    serializer = providers.Singleton(JsonSerializer)
    storage_providers = providers.Object({})

    runtime = providers.Singleton(
        GrainRuntime,
        storage_providers=storage_providers,
        serializer=serializer,
    )
    grain_factory = providers.Singleton(GrainFactory, runtime=runtime)
    timer_registry = providers.Singleton(TimerRegistry, runtime=runtime)
    logger = providers.Singleton(logging.getLogger, config.logger_name.as_(str))
