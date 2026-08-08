from __future__ import annotations

import asyncio

from common.core.exceptions import ServiceException
from common.logger import error, warn

from sandbox_v1.application.services.workspace_service import WorkspaceService
from sandbox_v1.domain.entities import (
    SandboxRecord,
    SandboxRecycleResult,
    SandboxRecycleStatus,
    SandboxState,
    WorkspaceLifecycleResult,
)
from sandbox_v1.domain.error_codes import SandboxErrorCode
from sandbox_v1.domain.interfaces.metrics import MetricsPort
from sandbox_v1.domain.interfaces.sandbox_provider import SandboxProvider
from sandbox_v1.domain.repositories import SandboxRepository


class SandboxLifecycleService:
    """Phase 4 lifecycle orchestration for recycle and permanent deletion.

    Pool ownership remains in Mongo. Starting recycle leaves the user binding in
    place while the sandbox is RETIRING/DESTROYING so concurrent calls see a
    deterministic sandbox_recycling response instead of accidentally receiving a
    fresh container before cleanup has completed.
    """

    def __init__(
        self,
        *,
        repository: SandboxRepository,
        provider: SandboxProvider,
        workspace_service: WorkspaceService,
        destroy_timeout_seconds: float,
        metrics: MetricsPort,
    ) -> None:
        self._repository = repository
        self._provider = provider
        self._workspace_service = workspace_service
        self._destroy_timeout = destroy_timeout_seconds
        self._metrics = metrics

    async def permanent_delete(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> WorkspaceLifecycleResult:
        user_id, session_id = self._validate_ids(user_id, session_id)
        record = await self._repository.begin_user_recycle(
            user_id,
            reason="permanent_delete",
        )
        if record is not None:
            await self._destroy_user_container(record, reason="permanent_delete")

        result = await self._workspace_service.permanent_delete(
            user_id=user_id,
            session_id=session_id,
        )
        self._metrics.increment("sandbox_permanent_deletes")
        return result

    async def start_recycle(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> SandboxRecycleResult:
        user_id, session_id = self._validate_ids(user_id, session_id)
        record = await self._repository.begin_user_recycle(
            user_id,
            reason="recycle_requested",
        )
        self._metrics.increment("sandbox_recycle_requests")
        return SandboxRecycleResult(
            user_id=user_id,
            session_id=session_id,
            status=SandboxRecycleStatus.RECYCLING,
            sandbox_id=record.ref.sandbox_id if record is not None else None,
        )

    async def finish_recycle(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> SandboxRecycleResult:
        user_id, session_id = self._validate_ids(user_id, session_id)
        record = await self._repository.begin_user_recycle(
            user_id,
            reason="recycle_worker",
        )
        snapshot_id: str | None = None
        if record is None:
            return SandboxRecycleResult(
                user_id=user_id,
                session_id=session_id,
                status=SandboxRecycleStatus.RECYCLING,
            )

        try:
            snapshot = await self._workspace_service.save_before_recycle(
                user_id=user_id,
                session_id=session_id,
            )
            snapshot_id = snapshot.snapshot_id if snapshot is not None else None
        except Exception as exc:
            # Recycle must not keep a container alive only because snapshotting
            # failed. The previous successful cache remains the recovery point.
            self._metrics.increment("workspace_recycle_snapshot_failures")
            warn(
                "workspace snapshot before recycle failed; destroying container",
                exc=exc,
                user_id=user_id,
                session_id=session_id,
                sandbox_id=record.ref.sandbox_id,
            )

        await self._destroy_user_container(record, reason="recycle")
        self._metrics.increment("sandbox_recycles_completed")
        return SandboxRecycleResult(
            user_id=user_id,
            session_id=session_id,
            status=SandboxRecycleStatus.RECYCLING,
            sandbox_id=record.ref.sandbox_id,
            snapshot_id=snapshot_id,
        )

    async def _destroy_user_container(
        self,
        record: SandboxRecord,
        *,
        reason: str,
    ) -> None:
        current = await self._repository.get(record.ref.sandbox_id)
        if current is None:
            await self._repository.clear_binding_for_sandbox(record.ref.sandbox_id)
            return
        if current.state in {SandboxState.DESTROYED, SandboxState.LOST}:
            await self._repository.clear_binding_for_sandbox(current.ref.sandbox_id)
            return
        if current.state == SandboxState.USER_ACTIVE:
            current = await self._repository.transition(
                current.ref.sandbox_id,
                SandboxState.USER_ACTIVE,
                SandboxState.RETIRING,
                error=reason,
            )
        if current.state == SandboxState.RETIRING:
            current = await self._repository.transition(
                current.ref.sandbox_id,
                SandboxState.RETIRING,
                SandboxState.DESTROYING,
                error=reason,
            )
        if current.state != SandboxState.DESTROYING:
            raise ServiceException(
                SandboxErrorCode.INVALID_STATE_TRANSITION,
                f"cannot destroy sandbox in {current.state.value}",
            )

        try:
            self._metrics.increment("destroy_attempts")
            await asyncio.wait_for(
                self._provider.destroy(current.ref, reason),
                timeout=self._destroy_timeout,
            )
        except Exception as exc:
            self._metrics.increment("destroy_failures")
            await self._mark_destroy_failed(current, exc)
            error(
                "user sandbox destroy failed",
                exc=exc,
                sandbox_id=current.ref.sandbox_id,
                reason=reason,
            )
            raise ServiceException(
                SandboxErrorCode.SANDBOX_UNAVAILABLE,
                "user sandbox destroy failed",
            ) from exc

        await self._repository.transition(
            current.ref.sandbox_id,
            SandboxState.DESTROYING,
            SandboxState.DESTROYED,
            error=reason,
        )
        await self._repository.clear_binding_for_sandbox(current.ref.sandbox_id)
        self._metrics.increment("destroy_successes")

    async def _mark_destroy_failed(
        self,
        record: SandboxRecord,
        exc: Exception,
    ) -> None:
        current = await self._repository.get(record.ref.sandbox_id)
        if current is None or current.state != SandboxState.DESTROYING:
            return
        try:
            await self._repository.transition(
                record.ref.sandbox_id,
                SandboxState.DESTROYING,
                SandboxState.LOST,
                error=str(exc)[:200],
            )
        except ServiceException:
            return

    @staticmethod
    def _validate_ids(user_id: str, session_id: str) -> tuple[str, str]:
        user_id = (user_id or "").strip()
        session_id = (session_id or "").strip()
        if not user_id or not session_id:
            raise ServiceException(
                SandboxErrorCode.INVALID_WORKSPACE_REQUEST,
                "user_id and session_id are required",
            )
        return user_id, session_id
