"""Dependency injection container for the pyleans framework."""

import logging

from dependency_injector import containers, providers

from pyleans.reference import GrainFactory
from pyleans.serialization import JsonSerializer, Serializer
from pyleans.server.providers.memory_stream import StreamManager
from pyleans.server.runtime import GrainRuntime
from pyleans.server.silo_management import SiloManagement
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
    stream_provider = providers.Object(None)

    runtime = providers.Singleton(
        GrainRuntime,
        storage_providers=storage_providers,
        serializer=serializer,
    )
    grain_factory = providers.Singleton(GrainFactory, runtime=runtime)
    timer_registry = providers.Singleton(TimerRegistry, runtime=runtime)
    silo_management = providers.Singleton(SiloManagement)
    stream_manager = providers.Singleton(StreamManager, provider=stream_provider)
    logger = providers.Singleton(logging.getLogger, config.logger_name.as_(str))
