from __future__ import annotations

import asyncio
import time
from datetime import timedelta, datetime, timezone

from common.logger import error, info
from sandbox.application.container_manager import ContainerManager, ContainerStatus
from sandbox.application.workspace_releaser import WorkspaceReleaser
from sandbox.core.config.app_settings import settings
from sandbox.core.providers import SandboxProviderManager
from sandbox.domain.interfaces import SandboxProviderInfo
from sandbox.domain.entities import SandboxDocument, SandboxState, WorkspaceState
from sandbox.domain.repositories import SandboxRepository, WorkspaceRepository


class Watcher:
    """后台容器池维护器
    """

    def __init__(
        self,
        sandbox_repository: SandboxRepository,
        workspace_repository: WorkspaceRepository,
        sandbox_provider_manager: SandboxProviderManager,
        container_manager: ContainerManager,
        workspace_releaser: WorkspaceReleaser,
    ) -> None:
        self._sandbox_repository = sandbox_repository
        self._workspace_repository = workspace_repository
        self._sandbox_provider_manager = sandbox_provider_manager
        self._container_manager = container_manager
        self._workspace_releaser = workspace_releaser
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self._last_workspace_release_at: datetime | None = None

    async def maintain_sandbox_pool(self) -> int:
        """检查沙箱池并按目标数量补充预热容器。"""

        # 同一时刻只允许进行一次容器状态检查
        async with self._lock:
            # 获取 MongoDB 中 就绪和正在预热的容器
            sandboxes = await self._sandbox_repository.get_by_states([SandboxState.READY, SandboxState.WARMING, SandboxState.DESTROYING])

            ready_count = 0
            warming_count = 0
            force_destroy_sandbox_list: list[SandboxDocument] = []
            for sandbox in sandboxes:

                if sandbox.state == SandboxState.READY:
                    sandbox_ready = await self._sandbox_provider_manager.check_ready(sandbox.provider_id, sandbox.base_url)
                    if sandbox_ready:
                        ready_count += 1
                    else:
                        force_destroy_sandbox_list.append(sandbox) # 强制销毁不健康的容器（由于未分配，可直接强制销毁）

                elif sandbox.state == SandboxState.WARMING:
                    # 检查正在预热的容器的实际状态
                    sandbox_ready = await self._sandbox_provider_manager.check_ready(sandbox.provider_id, sandbox.base_url)
                    if sandbox_ready: # 如果已经预热好，就新增就绪数，并更新状态
                        ready_count += 1
                        await self._sandbox_repository.change_state(sandbox.sandbox_id, SandboxState.READY)
                    elif datetime.now(timezone.utc) > sandbox.updated_at + timedelta(seconds=settings.SANDBOX_WARMUP_TIMEOUT_SECONDS):  # 超时
                        force_destroy_sandbox_list.append(sandbox)  # 强制销毁未能正常预热的容器（由于未分配，可直接强制销毁）
                    else:
                        warming_count += 1
                        # 没有就绪也没有超时的容器下次再检查

                elif sandbox.state == SandboxState.DESTROYING:
                    # 检查正在销毁的容器的实际状态
                    container_status = await self._container_manager.check_container_status(sandbox.container_id)
                    if container_status == ContainerStatus.NOT_FOUND:
                        await self._sandbox_repository.change_state(
                            sandbox.sandbox_id,
                            SandboxState.DESTROYED,
                            expected_state=SandboxState.DESTROYING,
                            clear_user_binding=True,
                        )
                    elif datetime.now(timezone.utc) > sandbox.updated_at + timedelta(seconds=settings.SANDBOX_DESTROY_TIMEOUT_SECONDS):
                            force_destroy_sandbox_list.append(sandbox)  # 强制销毁正在销毁的容器（不等待容器自然销毁）
                    # 没有超时的容器下次再检查

            if force_destroy_sandbox_list:
                await self.force_destroy_specified_sandbox(force_destroy_sandbox_list)

            existing = ready_count + warming_count
            if existing >= settings.SANDBOX_TARGET_READY:
                return 0 # 当前就绪和正在预热的容器数量超过了需要预热的总数量，不需要处理

            # 尝试预热容器到指定数量，连续三次失败即终止
            return await self.warm_sandboxes(settings.SANDBOX_TARGET_READY - existing)

    async def warm_sandboxes(self, plan_quantity: int) -> int:
        # 尝试预热容器到指定数量，连续三次失败即终止
        created = 0
        failures = 0

        provider_id = settings.SANDBOX_ACTIVE_PROVIDER_ID
        sandbox_provider_info: SandboxProviderInfo = self._sandbox_provider_manager.get_provider_info(provider_id)

        while created < plan_quantity and failures < settings.SANDBOX_WARMUP_MAX_RETRIES:
            container_id: str | None = None
            try:
                container_id = await self._container_manager.create(sandbox_provider_info.image)
                if container_id is None:
                    raise RuntimeError("container creation returned no id")
                base_url = await self._container_manager.get_container_base_url(container_id)
                sandbox = SandboxDocument(
                    container_id=container_id,
                    provider_id=provider_id.value,
                    base_url=base_url,
                    state=SandboxState.WARMING,
                    updated_at=datetime.now(timezone.utc)
                )
                await self._sandbox_repository.save(sandbox)
                created += 1
                failures = 0
            except Exception as exc:
                if container_id is not None:
                    try:
                        await self._container_manager.destroy(container_id)
                    except Exception as cleanup_exc:
                        error("sandbox warm cleanup failed", exc=cleanup_exc, container_id=container_id)
                failures += 1
                error("sandbox warm failed", exc=exc)

        return created

    async def force_destroy_specified_sandbox(self, sandboxes: list[SandboxDocument]) -> None:
        # 强制销毁指定的容器
        for sandbox in sandboxes:
            try:
                await self._container_manager.destroy(sandbox.container_id)
                await self._sandbox_repository.change_state(
                    sandbox.sandbox_id,
                    SandboxState.DESTROYED,
                    expected_state=sandbox.state,
                    clear_user_binding=True,
                )
            except Exception as exc:
                error("sandbox force destroy failed", exc=exc, sandbox_id=sandbox.sandbox_id)

    async def run(self) -> None:
        # 循环维护沙箱池
        while not self._stop.is_set():
            try:
                await self.maintain_sandbox_pool()
            except Exception as exc:
                error("sandbox watcher iteration failed", exc=exc)
            now = datetime.now(timezone.utc)
            if (
                self._last_workspace_release_at is None
                or (now - self._last_workspace_release_at).total_seconds()
                >= settings.SANDBOX_WORKSPACE_RELEASE_INTERVAL_SECONDS
            ):
                self._last_workspace_release_at = now
                try:
                    await self.release_idle_workspaces()
                except Exception as exc:
                    error("workspace release iteration failed", exc=exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=settings.SANDBOX_WATCHER_INTERVAL_SECONDS,
                )
            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        # 请求 watcher 循环停止
        self._stop.set()

    async def release_idle_workspaces(self) -> None:
        """按“续作导出任务 -> 抢占空闲工作区 -> 销毁空沙箱”顺序执行一轮释放。"""
        started = time.monotonic()
        stats = {"exporting": 0, "attached": 0, "claimed": 0, "released": 0, "destroyed": 0}
        sandbox_ids: set[str] = set()
        async with self._lock:
            # 先处理已经进入 EXPORTING 的记录：其中可能已经完成快照，只差删除容器目录。
            # 优先续作可以避免这些工作区长期占用沙箱运行时资源。
            exporting = await self._workspace_repository.list_by_states(
                [WorkspaceState.EXPORTING],
                settings.SANDBOX_WORKSPACE_RELEASE_BATCH_SIZE,
            )
            stats["exporting"] = len(exporting)
            for workspace in exporting:
                if workspace.sandbox_id:
                    sandbox_ids.add(workspace.sandbox_id)
                try:
                    await self._workspace_releaser.release_exporting(workspace)
                    stats["released"] += 1
                except Exception as exc:
                    error("workspace release failed", exc=exc, workspace_id=workspace.id, sandbox_id=workspace.sandbox_id)

            # 再扫描达到空闲阈值的 ATTACHED 工作区。查询结果只是候选，不能直接释放。
            # 用户请求可能在扫描后到达，因此必须用 state + last_accessed_at 做 CAS 抢占。
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.SANDBOX_WORKSPACE_IDLE_TIMEOUT_SECONDS)
            attached = await self._workspace_repository.list_idle_attached(cutoff, settings.SANDBOX_WORKSPACE_RELEASE_BATCH_SIZE)
            stats["attached"] = len(attached)
            for workspace in attached:
                if workspace.sandbox_id:
                    sandbox_ids.add(workspace.sandbox_id)
                try:
                    claimed = await self._workspace_repository.change_state(
                        workspace.id,
                        WorkspaceState.EXPORTING,
                        expected_state=WorkspaceState.ATTACHED,
                        expected_last_accessed_at=workspace.last_accessed_at,
                    )
                    if claimed is None:
                        # CAS 失败说明工作区已被访问或被其他流程处理，本轮跳过即可。
                        continue
                    stats["claimed"] += 1
                    await self._workspace_releaser.release_exporting(claimed)
                    stats["released"] += 1
                except Exception as exc:
                    error("idle workspace release failed", exc=exc, workspace_id=workspace.id, sandbox_id=workspace.sandbox_id)

            # 工作区释放完成后再按沙箱去重检查。只有没有 ATTACHED、EXPORTING、IMPORTING
            # 工作区时才允许进入 DESTROYING，避免删除仍被使用或仍在恢复中的容器。
            for sandbox_id in sandbox_ids:
                try:
                    if await self._destroy_sandbox_if_idle(sandbox_id):
                        stats["destroyed"] += 1
                except Exception as exc:
                    error("idle sandbox destroy failed", exc=exc, sandbox_id=sandbox_id)
        info("workspace release scan finished", **stats, duration_ms=round((time.monotonic() - started) * 1000, 2))

    async def _destroy_sandbox_if_idle(self, sandbox_id: str) -> bool:
        """仅当沙箱没有 ATTACHED/EXPORTING/IMPORTING 工作区时发起销毁。"""
        # count_runtime_workspaces 覆盖所有仍可能访问容器目录的状态，而不是只数 ATTACHED。
        if await self._workspace_repository.count_runtime_workspaces(sandbox_id) != 0:
            return False
        sandbox = await self._sandbox_repository.get_by_id(sandbox_id)
        if sandbox is None:
            return False
        # 通过 USER_ACTIVE -> DESTROYING CAS 抢占销毁权，避免与分配流程并发使用同一沙箱。
        destroying = await self._sandbox_repository.change_state(sandbox_id, SandboxState.DESTROYING, expected_state=SandboxState.USER_ACTIVE)
        if destroying is None:
            return False
        try:
            await self._container_manager.destroy(destroying.container_id)
        except Exception as exc:
            error("sandbox destroy failed", exc=exc, sandbox_id=sandbox_id, user_id=destroying.bind_user_id)
            return False
        # 容器删除成功后才清除用户绑定；失败时保留 DESTROYING，交给下一轮对账重试。
        await self._sandbox_repository.change_state(sandbox_id, SandboxState.DESTROYED, expected_state=SandboxState.DESTROYING, clear_user_binding=True)
        return True
