from __future__ import annotations

import asyncio
import re
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from common.core.exceptions import ServiceException
from common.logger import warn

from sandbox.domain.entities import WorkspaceExportBundleRef
from sandbox.domain.error_codes import SandboxErrorCode


_SAFE_WORKSPACE_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


class LocalWorkspaceCache:
    """管理宿主机工作区缓存，以 staging 和同文件系统 rename 原子替换完整目录。"""

    def __init__(self, root: str, max_bytes: int = 0) -> None:
        self._root = Path(root).expanduser().resolve()
        self._staging_root = self._root / ".staging"
        self._max_bytes = max(0, max_bytes)

    def cache_path(self, workspace_id: str) -> Path:
        """返回工作区唯一的正式缓存目录，并拒绝不安全的工作区 ID。"""
        if not _SAFE_WORKSPACE_ID.fullmatch(workspace_id or ""):
            raise ServiceException(SandboxErrorCode.WORKSPACE_PATH_INVALID, "workspace id is invalid")
        return self._root / workspace_id

    async def create_staging_directory(self, workspace_id: str) -> Path:
        """在缓存根目录内创建独占 staging 目录，供容器导出写入。"""
        def create_staging() -> Path:
            if not _SAFE_WORKSPACE_ID.fullmatch(workspace_id or ""):
                raise ServiceException(SandboxErrorCode.WORKSPACE_PATH_INVALID, "workspace id is invalid")
            self._staging_root.mkdir(parents=True, exist_ok=True)
            staging = self._staging_root / f"{workspace_id}-{uuid4().hex}"
            try:
                staging.mkdir(parents=False, exist_ok=False)
            except OSError as exc:
                raise ServiceException(SandboxErrorCode.WORKSPACE_SYNC_FAILED,f"workspace staging directory creation failed: {staging}") from exc
            return staging

        return await asyncio.to_thread(create_staging)

    async def install(
        self,
        workspace_id: str,
        staging_path: Path,
    ) -> WorkspaceExportBundleRef:
        """校验并统计 staging 内容，再原子安装为工作区的正式缓存。"""
        def install_staging() -> WorkspaceExportBundleRef:
            if not _SAFE_WORKSPACE_ID.fullmatch(workspace_id or ""):
                raise ServiceException(SandboxErrorCode.WORKSPACE_PATH_INVALID, "workspace id is invalid")
            raw_staging = staging_path.expanduser()
            if raw_staging.is_symlink():
                raise ServiceException(SandboxErrorCode.WORKSPACE_PATH_INVALID,f"workspace staging directory cannot be a symlink: {raw_staging}")
            staging = raw_staging.resolve()
            self._ensure_staging_path(staging)
            if not staging.is_dir():
                raise ServiceException(SandboxErrorCode.WORKSPACE_SYNC_FAILED,f"workspace staging directory does not exist: {staging}")

            file_count, directory_count, total_bytes = self._inspect_tree(staging)
            destination = self.cache_path(workspace_id)
            destination.parent.mkdir(parents=True, exist_ok=True)
            backup = destination.parent / f".{workspace_id}.backup-{uuid4().hex}"
            try:
                # 先保留旧缓存；新 staging 安装失败时可恢复，避免破坏最后一个可用副本。
                if destination.exists() or destination.is_symlink():
                    if destination.is_symlink():
                        raise ServiceException(SandboxErrorCode.WORKSPACE_PATH_INVALID,"workspace cache directory cannot be a symlink")
                    destination.rename(backup)
                staging.rename(destination)
            except Exception:
                if destination.exists():
                    shutil.rmtree(destination)
                if backup.exists() or backup.is_symlink():
                    backup.rename(destination)
                raise
            try:
                if backup.exists():
                    shutil.rmtree(backup)
            except OSError as exc:
                warn("workspace cache backup cleanup failed", path=str(backup), exc=exc)
            return WorkspaceExportBundleRef(
                id=uuid4().hex,
                workspace_id=workspace_id,
                bundle_path=str(destination),
                exported_at=datetime.now(timezone.utc),
                total_bytes=total_bytes,
                file_count=file_count,
                directory_count=directory_count,
            )

        return await asyncio.to_thread(install_staging)

    async def discard_staging(self, staging_path: Path) -> None:
        """删除本缓存组件创建的 staging 目录；拒绝缓存根目录之外的路径。"""
        def discard() -> None:
            staging = staging_path.expanduser().resolve()
            self._ensure_staging_path(staging)
            if staging.exists():
                shutil.rmtree(staging)

        await asyncio.to_thread(discard)

    def _inspect_tree(self, root: Path) -> tuple[int, int, int]:
        """验证 staging 内没有链接或特殊文件，并返回文件、目录和总字节数。"""
        file_count = 0
        directory_count = 0
        total_bytes = 0
        for path in root.rglob("*"):
            if path.is_symlink():
                raise ServiceException(SandboxErrorCode.WORKSPACE_PATH_INVALID,f"workspace cache cannot contain symlink: {path}")
            mode = path.stat(follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                directory_count += 1
            elif stat.S_ISREG(mode):
                file_count += 1
                total_bytes += path.stat(follow_symlinks=False).st_size
            else:
                raise ServiceException(SandboxErrorCode.WORKSPACE_PATH_INVALID,f"workspace cache contains unsupported file: {path}")
        if self._max_bytes and total_bytes > self._max_bytes:
            raise ServiceException(SandboxErrorCode.WORKSPACE_CACHE_LIMIT_EXCEEDED,f"workspace cache exceeds byte limit: {total_bytes}")
        return file_count, directory_count, total_bytes

    def _ensure_staging_path(self, staging: Path) -> None:
        """确保调用方不能借清理或安装操作访问 staging 根目录外的路径。"""
        try:
            staging.relative_to(self._staging_root.resolve())
        except ValueError as exc:
            raise ServiceException(SandboxErrorCode.WORKSPACE_PATH_INVALID,f"workspace staging path is outside cache staging root: {staging}") from exc
