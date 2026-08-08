from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends
from dependency_injector.wiring import Provide, inject

from common.core.domain import R
from sandbox_v1.api.schemas import SandboxRecycleResponse
from sandbox_v1.application.services.sandbox_lifecycle import SandboxLifecycleService
from sandbox_v1.container import Container


router = APIRouter(prefix="/internal/sandboxes", tags=["sandbox"])


@router.post(
    "/{user_id}/{session_id}/recycle",
    response_model=R[SandboxRecycleResponse],
    status_code=202,
    summary="Recycle the current user sandbox",
    description="""
Chat-only control operation. The request marks the current user container as
recycling and returns 202 immediately. Snapshot and destroy work continues in
the background; calls racing with the retained binding receive sandbox_recycling.
""",
)
@inject
async def recycle(
    user_id: str,
    session_id: str,
    background_tasks: BackgroundTasks,
    lifecycle_service: SandboxLifecycleService = Depends(
        Provide[Container.sandbox_lifecycle_service]
    ),
) -> R[SandboxRecycleResponse]:
    result = await lifecycle_service.start_recycle(
        user_id=user_id,
        session_id=session_id,
    )
    background_tasks.add_task(
        lifecycle_service.finish_recycle,
        user_id=user_id,
        session_id=session_id,
    )
    return R.success(data=SandboxRecycleResponse.from_result(result))
