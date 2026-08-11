from __future__ import annotations

from sandbox_v1.domain.entities import SandboxDocument, SandboxState
from sandbox_v1.domain.repositories import SandboxRepository


class SandboxBindingService:
    """最小用户绑定服务。"""

    def __init__(self, sandbox_repository: SandboxRepository) -> None:
        self._sandbox_repository = sandbox_repository

    async def bind_user(self, user_id: str) -> SandboxDocument:
        sandbox = await self._sandbox_repository.get_by_user_binding(user_id)
        if sandbox is not None and sandbox.state == SandboxState.USER_ACTIVE:
            return sandbox

        return await self._sandbox_repository.assign_to_user(user_id)
