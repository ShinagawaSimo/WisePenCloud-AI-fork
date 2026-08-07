from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path

import yaml
from common.logger import error, info
from pydantic import BaseModel, ConfigDict, model_validator

from sandbox_v1.core.config.bootstrap_settings import bootstrap_settings
from sandbox_v1.core.config.nacos import nacos_client_manager


class AppSettings(BaseModel):
    """Configuration for the sandbox v1 core service."""

    model_config = ConfigDict(extra="forbid")

    FROM_SOURCE_SECRET: str
    MONGODB_URL: str
    MONGODB_DB_NAME: str
    SANDBOX_IMAGE: str
    SANDBOX_MAX_USER_BINDINGS: int = 20
    SANDBOX_TARGET_READY: int
    SANDBOX_MIN_READY: int
    SANDBOX_READY_RESERVE: int = 0
    SANDBOX_MAX_CREATE_BATCH: int
    SANDBOX_WARMUP_TIMEOUT_SECONDS: float
    SANDBOX_DESTROY_TIMEOUT_SECONDS: float
    SANDBOX_WARMUP_MAX_RETRIES: int
    SANDBOX_WARMUP_RETRY_BACKOFF_SECONDS: float
    SANDBOX_WARMUP_RETRY_MAX_BACKOFF_SECONDS: float
    SANDBOX_WATCHER_INTERVAL_SECONDS: float
    SANDBOX_WORKSPACE_ROOT: str = "./data/workspaces"
    SANDBOX_WORKSPACE_CACHE_ROOT: str = "./data/workspace-cache"
    SANDBOX_WORKSPACE_SNAPSHOT_TTL_SECONDS: int = 7 * 24 * 60 * 60
    SANDBOX_WORKSPACE_CACHE_MAX_BYTES: int = 0
    SANDBOX_WORKSPACE_CACHE_HIGH_WATERMARK_RATIO: float = 0.8
    SANDBOX_WORKSPACE_CACHE_TARGET_WATERMARK_RATIO: float = 0.7
    SANDBOX_WORKSPACE_EVICTION_INTERVAL_SECONDS: float = 3600.0

    @model_validator(mode="after")
    def validate_runtime_values(self) -> "AppSettings":
        """Reject invalid pool settings before dependency wiring."""
        if self.SANDBOX_MIN_READY < 0 or self.SANDBOX_TARGET_READY < 0:
            raise ValueError("READY thresholds cannot be negative")
        if self.SANDBOX_MIN_READY > self.SANDBOX_TARGET_READY:
            raise ValueError("minimum READY cannot exceed target READY")
        if self.SANDBOX_READY_RESERVE < 0:
            raise ValueError("READY reserve cannot be negative")

        positive_values = (
            self.SANDBOX_MAX_USER_BINDINGS,
            self.SANDBOX_MAX_CREATE_BATCH,
            self.SANDBOX_WARMUP_TIMEOUT_SECONDS,
            self.SANDBOX_DESTROY_TIMEOUT_SECONDS,
            self.SANDBOX_WARMUP_MAX_RETRIES,
            self.SANDBOX_WARMUP_RETRY_BACKOFF_SECONDS,
            self.SANDBOX_WARMUP_RETRY_MAX_BACKOFF_SECONDS,
            self.SANDBOX_WATCHER_INTERVAL_SECONDS,
            self.SANDBOX_WORKSPACE_SNAPSHOT_TTL_SECONDS,
            self.SANDBOX_WORKSPACE_EVICTION_INTERVAL_SECONDS,
        )
        if any(value <= 0 for value in positive_values):
            raise ValueError("pool timeouts, limits, and capacities must be positive")
        if self.SANDBOX_WORKSPACE_CACHE_MAX_BYTES < 0:
            raise ValueError("workspace cache max bytes cannot be negative")
        if (
            self.SANDBOX_WARMUP_RETRY_MAX_BACKOFF_SECONDS
            < self.SANDBOX_WARMUP_RETRY_BACKOFF_SECONDS
        ):
            raise ValueError("maximum retry backoff cannot be below initial backoff")
        if not 0 < self.SANDBOX_WORKSPACE_CACHE_HIGH_WATERMARK_RATIO <= 1:
            raise ValueError("workspace cache high watermark must be in (0, 1]")
        if not (
            0 <= self.SANDBOX_WORKSPACE_CACHE_TARGET_WATERMARK_RATIO
            <= self.SANDBOX_WORKSPACE_CACHE_HIGH_WATERMARK_RATIO
        ):
            raise ValueError(
                "workspace cache target watermark cannot exceed high watermark"
            )
        if not self.FROM_SOURCE_SECRET.strip() or not self.SANDBOX_IMAGE.strip():
            raise ValueError("source secret and sandbox image are required")
        if not self.MONGODB_URL.strip() or not self.MONGODB_DB_NAME.strip():
            raise ValueError("MongoDB url and database name are required")
        if (
            not self.SANDBOX_WORKSPACE_ROOT.strip()
            or not self.SANDBOX_WORKSPACE_CACHE_ROOT.strip()
        ):
            raise ValueError("workspace root and cache root are required")
        return self


def _run_async(coro):
    """Run the async Nacos pull from the synchronous settings loader."""
    result, exc = None, None

    def _target() -> None:
        nonlocal result, exc
        try:
            result = asyncio.run(coro)
        except Exception as caught:
            exc = caught

    thread = threading.Thread(target=_target)
    thread.start()
    thread.join()
    if exc:
        raise exc
    return result


def _load_local() -> dict:
    service_root = Path(__file__).resolve().parents[4]
    cfg_path = service_root / "sandbox-v1-service.nacos.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(f"local config not found: {cfg_path}")
    raw = cfg_path.read_text(encoding="utf-8")
    config = yaml.safe_load(raw) or {}
    if not isinstance(config, dict):
        raise ValueError("local config must be a YAML mapping")
    info("using local sandbox-v1 core config", file=str(cfg_path))
    return config


def load_settings() -> AppSettings:
    use_nacos = str(os.getenv("CHAT_USE_NACOS") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    if bootstrap_settings.IS_DEV and not use_nacos:
        return AppSettings(**_load_local())

    try:
        raw_yaml = _run_async(nacos_client_manager.pull_config())
        if not raw_yaml or not raw_yaml.strip():
            raise RuntimeError("Nacos returned an empty sandbox config")
        config = yaml.safe_load(raw_yaml)
        if not isinstance(config, dict) or not config:
            raise RuntimeError("Nacos sandbox config must be a non-empty mapping")
        return AppSettings(**config)
    except Exception as exc:
        error("failed to load sandbox core config from Nacos", exc=exc)
        if bootstrap_settings.IS_DEV:
            return AppSettings(**_load_local())
        raise


settings = load_settings()
