from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from sandbox_v1.application.services.sandbox_binding import SandboxBindingService
from sandbox_v1.domain.entities import SandboxDocument, SessionWorkspaceDocument
from sandbox_v1.domain.repositories import WorkspaceRepository


class SandboxRequestService:
    """最小用户请求处理服务。"""

    def __init__(
        self,
        sandbox_binding_service: SandboxBindingService,
        workspace_repository: WorkspaceRepository,
    ) -> None:
        self._sandbox_binding_service = sandbox_binding_service
        self._workspace_repository = workspace_repository

    async def handle_user_request(
        self,
        user_id: str,
        session_id: str,
    ) -> tuple[SandboxDocument, SessionWorkspaceDocument]:
        sandbox = await self._sandbox_binding_service.bind_user(user_id)

        workspace = await self._workspace_repository.get_by_user_session(user_id, session_id)
        if workspace is not None:
            return sandbox, workspace

        workspace_id = uuid4().hex
        workspace_path = self._create_workspace_dir(workspace_id)
        workspace = SessionWorkspaceDocument.create_attached(
            workspace_id=workspace_id,
            user_id=user_id,
            session_id=session_id,
            sandbox_id=sandbox.sandbox_id,
            workspace_path=str(workspace_path),
        )
        await self._workspace_repository.save(workspace)
        return sandbox, workspace

    def _create_workspace_dir(self, workspace_id: str) -> Path:
        from sandbox_v1.core.config.app_settings import settings

        workspace_path = Path(settings.SANDBOX_WORKSPACE_ROOT) / workspace_id
        workspace_path.mkdir(parents=True, exist_ok=True)
        return workspace_path
