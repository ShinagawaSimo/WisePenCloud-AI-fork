from __future__ import annotations

from dependency_injector import containers, providers

from sandbox.application import ContainerManager, Watcher, WorkspaceAllocator, WorkspaceReclaimer
from sandbox.core.config.app_settings import settings
from sandbox.core.providers import AIOAdapter, SandboxProviderManager
from sandbox.core.storage.mongo import (
    MongoSandboxRepository,
    MongoWorkspaceRepository,
)
from sandbox.core.storage.local import LocalWorkspaceCache


def _mongo_client(url: str):
    from pymongo import AsyncMongoClient

    return AsyncMongoClient(url)


class Container(containers.DeclarativeContainer):
    """Sandbox dependency injection graph."""

    config = providers.Configuration()

    mongo_client = providers.Singleton(_mongo_client, url=config.MONGODB_URL)
    sandbox_repository = providers.Singleton(MongoSandboxRepository)
    workspace_repository = providers.Singleton(MongoWorkspaceRepository)

    sandbox_provider_manager = providers.Singleton(
        SandboxProviderManager,
        provider_classes=[AIOAdapter],
        provider_settings=settings.SANDBOX_PROVIDERS,
    )
    container_manager = providers.Singleton(
        ContainerManager
    )
    workspace_cache = providers.Singleton(
        LocalWorkspaceCache,
        root=settings.SANDBOX_WORKSPACE_CACHE_ROOT,
        max_bytes=settings.SANDBOX_WORKSPACE_CACHE_MAX_BYTES,
    )
    workspace_allocator = providers.Singleton(
        WorkspaceAllocator,
        sandbox_repository=sandbox_repository,
        workspace_repository=workspace_repository,
        container_manager=container_manager,
        workspace_cache=workspace_cache,
    )
    workspace_reclaimer = providers.Singleton(
        WorkspaceReclaimer,
        workspace_repository=workspace_repository,
        container_manager=container_manager,
        workspace_cache=workspace_cache,
    )
    watcher = providers.Singleton(
        Watcher,
        sandbox_repository=sandbox_repository,
        workspace_repository=workspace_repository,
        sandbox_provider_manager=sandbox_provider_manager,
        container_manager=container_manager,
        workspace_reclaimer=workspace_reclaimer,
    )


container = Container()
