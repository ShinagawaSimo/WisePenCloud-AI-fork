from __future__ import annotations

import asyncio
from pathlib import Path

from common.logger import error, info

from sandbox.application.container_manager import ContainerManager
from sandbox.core.config.app_settings import settings
from sandbox.core.storage.local import LocalWorkspaceCache
from sandbox.domain.entities import SessionWorkspaceDocument, WorkspaceExportBundleRef, WorkspaceState
from sandbox.domain.repositories import WorkspaceRepository


class WorkspaceReclaimer:
    """回收处于 EXPORTING 状态的工作区：缓存成功后删除容器目录，再解除运行时绑定。"""

    def __init__(
        self,
        workspace_repository: WorkspaceRepository,
        container_manager: ContainerManager,
        workspace_cache: LocalWorkspaceCache,
    ) -> None:
        self._workspace_repository = workspace_repository
        self._container_manager = container_manager
        self._workspace_cache = workspace_cache

    async def reclaim_exporting(self, workspace: SessionWorkspaceDocument) -> None:
        """继续一次可重试的回收任务，不让单个工作区失败中断后续扫描。"""
        if workspace.state != WorkspaceState.EXPORTING:
            return
        if not workspace.sandbox_id or not workspace.workspace_path:
            error("workspace reclaim missing runtime binding", workspace_id=workspace.id)
            return

        bundle = workspace.export_bundle
        if bundle is None:
            # 首次回收先导出缓存；缓存失败后仍清理容器目录，只有清理成功才能标记 LOST。
            bundle = await self._export_with_retries(workspace)
            if bundle is None:
                try:
                    await self._container_manager.remove_workspace_directory(workspace.sandbox_id, workspace.workspace_path)
                except Exception as exc:
                    error("workspace cleanup after cache failure failed", exc=exc, workspace_id=workspace.id)
                    return
                await self._workspace_repository.change_state(workspace.id, WorkspaceState.LOST, expected_state=WorkspaceState.EXPORTING, clear_runtime_binding=True)
                return
            # 先保存缓存引用，再删除容器目录，保证目录删除失败时下一轮可以续作。
            if await self._workspace_repository.change_state(workspace.id, WorkspaceState.EXPORTING, expected_state=WorkspaceState.EXPORTING, export_bundle=bundle) is None:
                return
        elif bundle.workspace_id != workspace.id or not self._workspace_cache.has_valid_bundle(bundle):
            error("workspace cache bundle is invalid", workspace_id=workspace.id)
            return

        # 已有缓存或缓存刚刚落库后，统一执行幂等的容器目录删除。
        try:
            await self._container_manager.remove_workspace_directory(workspace.sandbox_id, workspace.workspace_path)
        except Exception as exc:
            error("workspace directory removal failed", exc=exc, workspace_id=workspace.id, sandbox_id=workspace.sandbox_id)
            return

        if await self._workspace_repository.change_state(workspace.id, WorkspaceState.DETACHED, expected_state=WorkspaceState.EXPORTING, export_bundle=bundle, clear_runtime_binding=True) is not None:
            info("workspace reclaimed", workspace_id=workspace.id, sandbox_id=workspace.sandbox_id)

    async def _export_with_retries(
        self,
        workspace: SessionWorkspaceDocument,
    ) -> WorkspaceExportBundleRef | None:
        """导出到新的 staging 目录并原子安装缓存，达到重试上限时返回 None。"""
        attempts = settings.SANDBOX_WORKSPACE_CACHE_RETRY_COUNT
        for attempt in range(1, attempts + 1):
            staging: Path | None = None
            try:
                # 每次重试都使用全新的 staging，避免上一次部分复制的文件污染下一次导出。
                staging = await self._workspace_cache.create_staging_directory(workspace.id)
                await self._container_manager.export_workspace(workspace.sandbox_id, workspace.workspace_path, staging)
                return await self._workspace_cache.install(workspace.id, staging)
            except Exception as exc:
                error("workspace cache export failed", exc=exc, workspace_id=workspace.id, sandbox_id=workspace.sandbox_id, attempt=attempt)
                if attempt < attempts and settings.SANDBOX_WORKSPACE_CACHE_RETRY_BACKOFF_SECONDS:
                    # 退避只发生在还有后续机会时，最后一次失败直接进入 LOST 处理。
                    await asyncio.sleep(settings.SANDBOX_WORKSPACE_CACHE_RETRY_BACKOFF_SECONDS)
            finally:
                # install 成功会移动 staging；清理不存在的目录是幂等的。
                if staging is not None:
                    try:
                        await self._workspace_cache.discard_staging(staging)
                    except Exception as exc:
                        error("workspace staging cleanup failed", exc=exc, workspace_id=workspace.id, attempt=attempt)
        return None
