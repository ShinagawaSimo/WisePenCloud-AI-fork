from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from common.core.exceptions import ServiceException

from fake_mongo import FakeDatabase
from sandbox_v1.application.services.sandbox_lifecycle import SandboxLifecycleService
from sandbox_v1.application.services.sandbox_pool import SandboxPool
from sandbox_v1.application.services.workspace_service import WorkspaceService
from sandbox_v1.core.observability import MetricsCollector
from sandbox_v1.core.storage.filesystem import LocalWorkspaceSnapshotCache
from sandbox_v1.core.storage.mongo import (
    MongoSandboxRepository,
    MongoWorkspaceRepository,
)
from sandbox_v1.domain.entities import (
    DiscoveredSandbox,
    Endpoint,
    Health,
    SandboxRecycleStatus,
    SandboxRecord,
    SandboxRef,
    SandboxSpec,
    SandboxState,
    WorkspaceLifecycleStatus,
    WorkspaceState,
)
from sandbox_v1.domain.error_codes import SandboxErrorCode


@dataclass
class RecordingProvider:
    destroyed: list[tuple[str, str]] = field(default_factory=list)

    async def validate_deployment(self) -> None:
        return None

    async def create(self, spec: SandboxSpec) -> SandboxRef:
        raise NotImplementedError

    async def wait_ready(self, sandbox: SandboxRef, timeout_seconds: float) -> Health:
        return Health(healthy=True, status="ready")

    async def health(self, sandbox: SandboxRef) -> Health:
        return Health(healthy=True, status="ready")

    async def list_managed(self) -> list[DiscoveredSandbox]:
        return []

    async def cleanup_owned(self) -> int:
        return 0

    async def destroy(self, sandbox: SandboxRef, reason: str) -> None:
        self.destroyed.append((sandbox.sandbox_id, reason))


async def _seed_user_container(
    repository: MongoSandboxRepository,
    pool: SandboxPool,
    *,
    user_id: str,
    sandbox_id: str,
) -> SandboxRecord:
    await repository.save(
        SandboxRecord(
            ref=SandboxRef(
                sandbox_id=sandbox_id,
                provider_id=f"provider-{sandbox_id}",
                endpoint=Endpoint(base_url="http://127.0.0.1:8080"),
            ),
            state=SandboxState.READY,
        )
    )
    return await pool.consume(user_id)


def _workspace_service(
    tmp_path: Path,
    repository: MongoWorkspaceRepository,
) -> WorkspaceService:
    return WorkspaceService(
        repository=repository,
        cache=LocalWorkspaceSnapshotCache(cache_root=tmp_path / "cache"),
        workspace_root=tmp_path / "workspaces",
        metrics=MetricsCollector(),
    )


def _lifecycle_service(
    *,
    repository: MongoSandboxRepository,
    provider: RecordingProvider,
    workspace_service: WorkspaceService,
    metrics: MetricsCollector | None = None,
) -> SandboxLifecycleService:
    return SandboxLifecycleService(
        repository=repository,
        provider=provider,
        workspace_service=workspace_service,
        destroy_timeout_seconds=1,
        metrics=metrics or MetricsCollector(),
    )


@pytest.mark.asyncio
async def test_permanent_delete_purges_workspace_cache_and_blocks_rebuild(
    tmp_path: Path,
) -> None:
    sandbox_repository = MongoSandboxRepository(database=FakeDatabase())
    workspace_repository = MongoWorkspaceRepository(database=FakeDatabase())
    await sandbox_repository.initialize()
    await workspace_repository.initialize()
    pool = SandboxPool(sandbox_repository, min_ready=1, target_ready=1)
    provider = RecordingProvider()
    workspace_service = _workspace_service(tmp_path, workspace_repository)
    lifecycle = _lifecycle_service(
        repository=sandbox_repository,
        provider=provider,
        workspace_service=workspace_service,
    )

    await _seed_user_container(
        sandbox_repository,
        pool,
        user_id="user-a",
        sandbox_id="sandbox-a",
    )
    workspace_key = workspace_service.workspace_key("user-a", "session-a")
    workspace_path = tmp_path / "workspaces" / workspace_key
    workspace_path.mkdir(parents=True)
    (workspace_path / "answer.txt").write_text("42", encoding="utf-8")
    snapshot = await workspace_service.save_before_recycle(
        user_id="user-a",
        session_id="session-a",
    )
    assert snapshot is not None
    assert (tmp_path / "cache" / "snapshots" / workspace_key).exists()

    deleted = await lifecycle.permanent_delete(
        user_id="user-a",
        session_id="session-a",
    )

    assert deleted.status == WorkspaceLifecycleStatus.DELETED
    assert provider.destroyed == [("sandbox-a", "permanent_delete")]
    assert not workspace_path.exists()
    assert not (tmp_path / "cache" / "snapshots" / workspace_key).exists()

    record = await workspace_repository.get("user-a", "session-a")
    assert record is not None
    assert record.state == WorkspaceState.DELETED
    assert record.permanently_deleted_at is not None
    assert record.tombstone_snapshot is None

    repeat = await lifecycle.permanent_delete(
        user_id="user-a",
        session_id="session-a",
    )
    assert repeat.status == WorkspaceLifecycleStatus.DELETED
    assert provider.destroyed == [("sandbox-a", "permanent_delete")]

    rebuilt = await workspace_service.rebuild(
        user_id="user-a",
        session_id="session-a",
    )
    assert rebuilt.status == WorkspaceLifecycleStatus.DELETED
    assert not workspace_path.exists()


@pytest.mark.asyncio
async def test_recycle_keeps_binding_busy_then_saves_and_destroys(
    tmp_path: Path,
) -> None:
    sandbox_repository = MongoSandboxRepository(database=FakeDatabase())
    workspace_repository = MongoWorkspaceRepository(database=FakeDatabase())
    await sandbox_repository.initialize()
    await workspace_repository.initialize()
    pool = SandboxPool(sandbox_repository, min_ready=1, target_ready=1)
    provider = RecordingProvider()
    workspace_service = _workspace_service(tmp_path, workspace_repository)
    lifecycle = _lifecycle_service(
        repository=sandbox_repository,
        provider=provider,
        workspace_service=workspace_service,
    )

    await _seed_user_container(
        sandbox_repository,
        pool,
        user_id="user-b",
        sandbox_id="sandbox-b",
    )
    workspace_key = workspace_service.workspace_key("user-b", "session-b")
    workspace_path = tmp_path / "workspaces" / workspace_key
    workspace_path.mkdir(parents=True)
    (workspace_path / "note.txt").write_text("keep", encoding="utf-8")

    started = await lifecycle.start_recycle(
        user_id="user-b",
        session_id="session-b",
    )

    assert started.status == SandboxRecycleStatus.RECYCLING
    assert started.sandbox_id == "sandbox-b"
    with pytest.raises(ServiceException) as exc:
        await pool.consume("user-b")
    assert exc.value.code == SandboxErrorCode.SANDBOX_RECYCLING.code

    finished = await lifecycle.finish_recycle(
        user_id="user-b",
        session_id="session-b",
    )

    assert finished.status == SandboxRecycleStatus.RECYCLING
    assert finished.snapshot_id is not None
    assert provider.destroyed == [("sandbox-b", "recycle")]
    recycled = await sandbox_repository.get("sandbox-b")
    assert recycled is not None
    assert recycled.state == SandboxState.DESTROYED
    workspace_record = await workspace_repository.get("user-b", "session-b")
    assert workspace_record is not None
    assert workspace_record.tombstone_snapshot is not None

    await _seed_user_container(
        sandbox_repository,
        pool,
        user_id="user-b",
        sandbox_id="sandbox-b2",
    )
    reused = await pool.consume("user-b")
    assert reused.ref.sandbox_id == "sandbox-b2"
