from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import PurePosixPath
from uuid import uuid4

from common.core.exceptions import ServiceException

from sandbox.application.container_manager import ContainerManager
from sandbox.core.config.app_settings import settings
from sandbox.core.storage.local import LocalWorkspaceCache
from sandbox.domain.entities import SandboxDocument, SandboxState, SessionWorkspaceDocument, WorkspaceState
from sandbox.domain.error_codes import SandboxErrorCode
from sandbox.domain.repositories import SandboxRepository, WorkspaceRepository


class WorkspaceAllocator:
    """协调用户沙箱与会话工作区的分配，并与后台回收状态机保持 CAS 一致。"""

    def __init__(
        self,
        sandbox_repository: SandboxRepository,
        workspace_repository: WorkspaceRepository,
        container_manager: ContainerManager,
        workspace_cache: LocalWorkspaceCache,
    ) -> None:
        self._sandbox_repository = sandbox_repository
        self._workspace_repository = workspace_repository
        self._container_manager = container_manager
        self._workspace_cache = workspace_cache

    async def allocate(self, user_id: str, session_id: str) -> str:
        """返回可用工作区；转场中的记录等待完成，避免并发请求制造重复工作区。"""
        workspace = await self._workspace_repository.get_by_user_session(user_id, session_id)
        deadline = asyncio.get_running_loop().time() + settings.SANDBOX_WORKSPACE_TRANSITION_WAIT_TIMEOUT_SECONDS
        while workspace and workspace.state in (WorkspaceState.EXPORTING, WorkspaceState.IMPORTING):
            if asyncio.get_running_loop().time() >= deadline:
                raise ServiceException(SandboxErrorCode.WORKSPACE_TRANSITION_IN_PROGRESS)
            await asyncio.sleep(settings.SANDBOX_WORKSPACE_TRANSITION_POLL_INTERVAL_SECONDS)
            workspace = await self._workspace_repository.get_by_id(workspace.id)

        if workspace and workspace.state == WorkspaceState.DETACHED:
            bundle = workspace.export_bundle
            if bundle is None or not self._workspace_cache.has_valid_bundle(bundle):
                changed = await self._workspace_repository.change_state(workspace.id, WorkspaceState.LOST, expected_state=WorkspaceState.DETACHED)
                if changed is None:
                    raise ServiceException(SandboxErrorCode.WORKSPACE_TRANSITION_IN_PROGRESS)
                workspace = changed

        if workspace and workspace.state == WorkspaceState.ATTACHED:
            touched = await self._workspace_repository.touch_if_attached(workspace.id)
            if touched is not None:
                return workspace.id
            raise ServiceException(SandboxErrorCode.WORKSPACE_TRANSITION_IN_PROGRESS)

        if workspace and workspace.state == WorkspaceState.DETACHED:
            importing = await self._workspace_repository.change_state(workspace.id, WorkspaceState.IMPORTING, expected_state=WorkspaceState.DETACHED)
            if importing is None:
                raise ServiceException(SandboxErrorCode.WORKSPACE_TRANSITION_IN_PROGRESS)
            return await self._restore_workspace(importing, user_id)

        sandbox = await self._sandbox_repository.get_by_user_binding(user_id)
        if sandbox is None or sandbox.state != SandboxState.USER_ACTIVE:
            sandbox = await self._sandbox_repository.assign_to_user(user_id)
        return await self._create_workspace(workspace, sandbox, user_id, session_id)

    async def _restore_workspace(self, workspace: SessionWorkspaceDocument, user_id: str) -> str:
        """从本地缓存恢复工作区；数据库 CAS 失败时清理本次容器目录。"""
        sandbox: SandboxDocument | None = None
        workspace_path = str(PurePosixPath(settings.SANDBOX_CONTAINER_WORKSPACE_ROOT) / workspace.id)
        try:
            sandbox = await self._sandbox_repository.get_by_user_binding(user_id)
            if sandbox is None or sandbox.state != SandboxState.USER_ACTIVE:
                sandbox = await self._sandbox_repository.assign_to_user(user_id)
            restored_path = await self._container_manager.restore_cached_workspace(sandbox.container_id, workspace.id)
            attached = await self._workspace_repository.set_attached_workspace(workspace.id, sandbox.sandbox_id, restored_path, expected_state=WorkspaceState.IMPORTING)
            if attached is None:
                raise ServiceException(SandboxErrorCode.WORKSPACE_TRANSITION_IN_PROGRESS)
            return workspace.id
        except Exception:
            if sandbox is not None:
                try:
                    await self._container_manager.remove_workspace_directory(sandbox.container_id, workspace_path)
                except Exception:
                    # 清理失败不覆盖原始恢复错误；下一轮仍会依据 IMPORTING 状态重试对账。
                    pass
            await self._workspace_repository.change_state(workspace.id, WorkspaceState.DETACHED, expected_state=WorkspaceState.IMPORTING)
            raise

    async def _create_workspace(self, existing: SessionWorkspaceDocument | None, sandbox: SandboxDocument,
                                user_id: str, session_id: str) -> str:
        """创建新容器目录并持久化 ATTACHED 记录；保存失败时清理孤儿目录。"""
        workspace_id = existing.id if existing and existing.state == WorkspaceState.LOST else uuid4().hex
        workspace_path = await self._container_manager.create_workspace_directory(sandbox.container_id, workspace_id)
        now = datetime.now(timezone.utc)
        try:
            await self._workspace_repository.save(SessionWorkspaceDocument(
                id=workspace_id, user_id=user_id, session_id=session_id,
                state=WorkspaceState.ATTACHED, sandbox_id=sandbox.sandbox_id,
                workspace_path=workspace_path, created_at=existing.created_at if existing else now,
                updated_at=now, last_accessed_at=now,
            ))
            return workspace_id
        except Exception:
            await self._container_manager.remove_workspace_directory(sandbox.container_id, workspace_path)
            raise
