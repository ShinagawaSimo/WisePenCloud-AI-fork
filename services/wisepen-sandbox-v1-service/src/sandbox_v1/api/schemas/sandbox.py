from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from sandbox_v1.domain.entities import SandboxRecycleResult, SandboxRecycleStatus


class SandboxRecycleResponse(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    user_id: str = Field(..., min_length=1)
    session_id: str = Field(..., min_length=1)
    status: SandboxRecycleStatus
    sandbox_id: str | None = None
    snapshot_id: str | None = None

    @classmethod
    def from_result(cls, result: SandboxRecycleResult) -> "SandboxRecycleResponse":
        return cls(
            user_id=result.user_id,
            session_id=result.session_id,
            status=result.status,
            sandbox_id=result.sandbox_id,
            snapshot_id=result.snapshot_id,
        )
