from __future__ import annotations

from pathlib import Path

import pytest

from sandbox_v1.core.storage.mongo import (
    MongoSandboxRepository,
    MongoWorkspaceRepository,
)
from sandbox_v1.domain.entities import (
    Endpoint,
    SandboxRecord,
    SandboxRef,
    SandboxState,
    WorkspaceRestoreStartStatus,
    WorkspaceSnapshotRef,
    WorkspaceState,
)
from fake_mongo import FakeDatabase


@pytest.mark.asyncio
async def test_mongo_sandbox_repository_checkouts_are_persistent() -> None:
    database = FakeDatabase()
    repository = MongoSandboxRepository(database=database)
    await repository.initialize()
    await repository.save(
        SandboxRecord(
            ref=SandboxRef(
                sandbox_id="sandbox-a",
                provider_id="container-a",
                endpoint=Endpoint(base_url="http://127.0.0.1:8080"),
            ),
            state=SandboxState.READY,
        )
    )

    consumed = await repository.checkout_ready("user-a")
    reused = await repository.checkout_ready("user-a")
    snapshot = await repository.snapshot(min_ready=1, target_ready=1)

    assert consumed.state == SandboxState.USER_ACTIVE
    assert reused.reuse_count == 1
    assert snapshot.counts[SandboxState.USER_ACTIVE] == 1
    assert snapshot.generation >= 3


@pytest.mark.asyncio
async def test_mongo_workspace_repository_marks_restoring_once(tmp_path: Path) -> None:
    database = FakeDatabase()
    repository = MongoWorkspaceRepository(database=database)
    await repository.initialize()
    record = await repository.ensure_active(
        user_id="user-a",
        session_id="session-a",
        workspace_key="workspace-a",
        workspace_path=str(tmp_path),
    )

    deleting = await repository.begin_delete(
        user_id="user-a",
        session_id="session-a",
        workspace_key="workspace-a",
        workspace_path=str(tmp_path),
    )
    deleted = await repository.finish_delete(
        user_id="user-a",
        session_id="session-a",
        snapshot=WorkspaceSnapshotRef(
            workspace_key="workspace-a",
            snapshot_id="snapshot-a",
        ),
    )
    first_restore = await repository.begin_restore(
        user_id="user-a",
        session_id="session-a",
        workspace_key="workspace-a",
        workspace_path=str(tmp_path),
    )
    second_restore = await repository.begin_restore(
        user_id="user-a",
        session_id="session-a",
        workspace_key="workspace-a",
        workspace_path=str(tmp_path),
    )

    assert record.state == WorkspaceState.ACTIVE
    assert deleting.state == WorkspaceState.DELETING
    assert deleted.state == WorkspaceState.DELETED
    assert first_restore.status == WorkspaceRestoreStartStatus.STARTED
    assert second_restore.status == WorkspaceRestoreStartStatus.RESTORING
