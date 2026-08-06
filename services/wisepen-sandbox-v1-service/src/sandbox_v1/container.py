from __future__ import annotations

from dependency_injector import containers, providers

from sandbox_v1.application.services.sandbox_pool import SandboxPool
from sandbox_v1.application.services.sandbox_startup_reconciler import (
    SandboxStartupReconciler,
)
from sandbox_v1.application.services.sandbox_watcher import Watcher
from sandbox_v1.application.services.workspace_eviction import WorkspaceEvictionWorker
from sandbox_v1.application.services.workspace_service import WorkspaceService
from sandbox_v1.core.observability import MetricsCollector
from sandbox_v1.core.storage.filesystem import LocalWorkspaceSnapshotCache
from sandbox_v1.core.storage.memory import (
    MemorySandboxRepository,
    MemoryWorkspaceRepository,
)
from sandbox_v1.domain.entities import SandboxSpec


def _sandbox_spec(image: str) -> SandboxSpec:
    """Build the provider-neutral spec used by the pool replenisher."""
    return SandboxSpec(image=image)


class Container(containers.DeclarativeContainer):
    """Dependency graph for the container-pool core.

    The concrete runtime provider is deliberately a dependency. Docker/AIO
    selection belongs to a later integration layer and is not part of this
    service's core implementation.
    """

    config = providers.Configuration()

    metrics = providers.Singleton(MetricsCollector)
    repository = providers.Singleton(MemorySandboxRepository, metrics=metrics)
    workspace_repository = providers.Singleton(MemoryWorkspaceRepository)
    workspace_cache = providers.Singleton(
        LocalWorkspaceSnapshotCache,
        cache_root=config.SANDBOX_WORKSPACE_CACHE_ROOT,
        ttl_seconds=config.SANDBOX_WORKSPACE_SNAPSHOT_TTL_SECONDS,
        max_bytes=config.SANDBOX_WORKSPACE_CACHE_MAX_BYTES,
        high_watermark_ratio=config.SANDBOX_WORKSPACE_CACHE_HIGH_WATERMARK_RATIO,
        target_watermark_ratio=config.SANDBOX_WORKSPACE_CACHE_TARGET_WATERMARK_RATIO,
    )
    workspace_service = providers.Singleton(
        WorkspaceService,
        repository=workspace_repository,
        cache=workspace_cache,
        workspace_root=config.SANDBOX_WORKSPACE_ROOT,
        metrics=metrics,
    )
    workspace_eviction_worker = providers.Singleton(
        WorkspaceEvictionWorker,
        workspace_service=workspace_service,
        interval_seconds=config.SANDBOX_WORKSPACE_EVICTION_INTERVAL_SECONDS,
    )
    pool = providers.Singleton(
        SandboxPool,
        repository=repository,
        min_ready=config.SANDBOX_MIN_READY,
        target_ready=config.SANDBOX_TARGET_READY,
        max_user_bindings=config.SANDBOX_MAX_USER_BINDINGS,
    )

    # Deployment code must override this port with its container runtime.
    provider = providers.Dependency()
    startup_reconciler = providers.Singleton(
        SandboxStartupReconciler,
        repository=repository,
        provider=provider,
    )
    watcher = providers.Singleton(
        Watcher,
        pool=pool,
        repository=repository,
        provider=provider,
        spec=providers.Factory(_sandbox_spec, image=config.SANDBOX_IMAGE),
        min_ready=config.SANDBOX_MIN_READY,
        reserve=config.SANDBOX_READY_RESERVE,
        max_create_batch=config.SANDBOX_MAX_CREATE_BATCH,
        warmup_timeout_seconds=config.SANDBOX_WARMUP_TIMEOUT_SECONDS,
        destroy_timeout_seconds=config.SANDBOX_DESTROY_TIMEOUT_SECONDS,
        interval_seconds=config.SANDBOX_WATCHER_INTERVAL_SECONDS,
        warmup_max_retries=config.SANDBOX_WARMUP_MAX_RETRIES,
        warmup_retry_backoff_seconds=config.SANDBOX_WARMUP_RETRY_BACKOFF_SECONDS,
        warmup_retry_max_backoff_seconds=config.SANDBOX_WARMUP_RETRY_MAX_BACKOFF_SECONDS,
        metrics=metrics,
    )


container = Container()
