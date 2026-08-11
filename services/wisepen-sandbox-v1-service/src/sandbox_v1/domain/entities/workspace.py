from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4

from beanie import Document
from pydantic import BaseModel, Field
from pymongo import ASCENDING, IndexModel


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class WorkspaceState(StrEnum):
    """Workspace 在导出、挂起、导入过程中的生命周期状态"""

    ATTACHED = "attached"
    EXPORTING = "exporting"
    DETACHED = "detached"
    IMPORTING = "importing"


class WorkspaceExportBundleRef(BaseModel):
    """容器释放后导出的 workspace 数据包引用"""

    id: str = Field(..., description="工作区导出包 ID")
    workspace_id: str = Field(..., description="所属 workspace ID")

    bundle_path: str | None = Field(default=None, description="当前导出包的物理路径")

    exported_at: datetime = Field(default_factory=_utc_now, description="导出时间")
    total_bytes: int = Field(default=0, description="导出包总大小，单位字节")
    file_count: int = Field(default=0, description="导出包中的文件数")
    directory_count: int = Field(default=0, description="导出包中的目录数")

class SessionWorkspaceDocument(Document):
    """Session Workspace 权威记录"""

    id: str = Field(..., description="工作区 ID")
    user_id: str = Field(..., description="所属用户 ID")
    session_id: str = Field(..., description="所属会话 ID")
    sandbox_id: str | None = Field(default=None, description="绑定的沙箱 ID")
    state: WorkspaceState = Field(default=WorkspaceState.ATTACHED, description="当前 workspace 状态")

    workspace_path: str | None = Field(default=None, description="当前 workspace 的物理路径") # 仅当其在容器中时存在

    export_bundle: WorkspaceExportBundleRef | None = Field(default=None, description="导出的 workspace 数据包")

    created_at: datetime = Field(default_factory=_utc_now, description="创建时间")
    updated_at: datetime = Field(default_factory=_utc_now, description="最近更新时间")
    last_accessed_at: datetime = Field(default_factory=_utc_now, description="最近一次访问时间")

    @classmethod
    def create_attached(
        cls,
        *,
        user_id: str,
        session_id: str,
        sandbox_id: str,
        workspace_path: str,
        workspace_id: str | None = None,
    ) -> "SessionWorkspaceDocument":
        now = _utc_now()
        return cls(
            id=workspace_id or uuid4().hex,
            user_id=user_id,
            session_id=session_id,
            sandbox_id=sandbox_id,
            workspace_path=workspace_path,
            state=WorkspaceState.ATTACHED,
            created_at=now,
            updated_at=now,
            last_accessed_at=now,
        )

    class Settings:
        name = "wisepen_sandbox_v1_session_workspace"
