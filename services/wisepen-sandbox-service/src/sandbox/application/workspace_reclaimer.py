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
        if bundle is not None:
            # 已落库的缓存表示此前仅目录删除失败，不能重新导出并覆盖该缓存。
            expected_path = self._workspace_cache.cache_path(workspace.id)
            bundle_path = Path(bundle.bundle_path).expanduser().resolve() if bundle.bundle_path else None
            if (
                bundle.workspace_id != workspace.id
                or bundle_path != expected_path.resolve()
                or not expected_path.is_dir()
                or expected_path.is_symlink()
            ):
                error("workspace cache bundle is invalid", workspace_id=workspace.id)
                return
        else:
            bundle = await self._export_with_retries(workspace)
            if bundle is None:
                # 缓存重试耗尽后仍删除容器目录；只有目录清理成功才能标记为 LOST。
                try:
                    await self._container_manager.remove_workspace_directory(
                        workspace.sandbox_id,
                        workspace.workspace_path,
                    )
                except Exception as exc:
                    error("workspace cleanup after cache failure failed", exc=exc, workspace_id=workspace.id)
                    return
                await self._workspace_repository.change_state(
                    workspace.id,
                    WorkspaceState.LOST,
                    expected_state=WorkspaceState.EXPORTING,
                    clear_runtime_binding=True,
                )
                return
            persisted = await self._workspace_repository.change_state(
                workspace.id,
                WorkspaceState.EXPORTING,
                expected_state=WorkspaceState.EXPORTING,
                export_bundle=bundle,
            )
            if persisted is None:
                return

        # 缓存引用已持久化后才删除容器目录，避免导出成功但数据库无恢复入口。
        try:
            await self._container_manager.remove_workspace_directory(workspace.sandbox_id, workspace.workspace_path)
        except Exception as exc:
            error("workspace directory removal failed", exc=exc, workspace_id=workspace.id, sandbox_id=workspace.sandbox_id)
            return

        detached = await self._workspace_repository.change_state(
            workspace.id,
            WorkspaceState.DETACHED,
            expected_state=WorkspaceState.EXPORTING,
            export_bundle=bundle,
            clear_runtime_binding=True,
        )
        if detached is not None:
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
                staging = await self._workspace_cache.create_staging_directory(workspace.id)
                await self._container_manager.export_workspace(workspace.sandbox_id, workspace.workspace_path, staging)
                return await self._workspace_cache.install(workspace.id, staging)
            except Exception as exc:
                error("workspace cache export failed", exc=exc, workspace_id=workspace.id, sandbox_id=workspace.sandbox_id, attempt=attempt)
                if attempt < attempts and settings.SANDBOX_WORKSPACE_CACHE_RETRY_BACKOFF_SECONDS:
                    await asyncio.sleep(settings.SANDBOX_WORKSPACE_CACHE_RETRY_BACKOFF_SECONDS)
            finally:
                # install 成功会移动 staging；清理不存在的目录是幂等的。
                if staging is not None:
                    await self._workspace_cache.discard_staging(staging)
        return None
