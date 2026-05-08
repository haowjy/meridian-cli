"""Stable hook contract types owned by the plugin API."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import UUID

HookEventName = Literal[
    "spawn.created",
    "spawn.running",
    "spawn.start",
    "spawn.finalized",
    "work.start",
    "work.started",
    "work.done",
]

HookOutcome = Literal["success", "failure", "timeout", "skipped"]
FailurePolicy = Literal["fail", "warn", "ignore"]
SpawnStatus = Literal["success", "failure", "cancelled", "timeout", "skipped"]


def _default_options() -> dict[str, object]:
    return {}


@dataclass(frozen=True)
class Hook:
    """Plugin-facing hook configuration shape."""

    name: str
    event: HookEventName
    source: str
    builtin: str | None = None
    command: str | None = None
    enabled: bool = True
    priority: int = 0
    require_serial: bool = False
    exclude: tuple[str, ...] = ()
    options: Mapping[str, object] = field(default_factory=_default_options)
    failure_policy: FailurePolicy | None = None
    remote: str | None = None


@dataclass(frozen=True)
class HookContext:
    """Plugin-facing structured hook context."""

    event_name: HookEventName
    event_id: UUID
    timestamp: str
    project_root: str
    runtime_root: str
    schema_version: int = 1

    spawn_id: str | None = None
    spawn_status: SpawnStatus | None = None
    spawn_agent: str | None = None
    spawn_model: str | None = None
    spawn_duration_secs: float | None = None
    spawn_cost_usd: float | None = None
    spawn_error: str | None = None

    work_id: str | None = None
    work_dir: str | None = None

    @classmethod
    def from_roots(
        cls,
        *,
        project_root: str | Path,
        runtime_root: str | Path,
        event_name: HookEventName,
        event_id: UUID,
        timestamp: str,
        spawn_id: str | None = None,
        spawn_status: SpawnStatus | None = None,
        spawn_agent: str | None = None,
        spawn_model: str | None = None,
        spawn_duration_secs: float | None = None,
        spawn_cost_usd: float | None = None,
        spawn_error: str | None = None,
        work_id: str | None = None,
        work_dir: str | Path | None = None,
    ) -> HookContext:
        return cls(
            event_name=event_name,
            event_id=event_id,
            timestamp=timestamp,
            project_root=Path(project_root).as_posix(),
            runtime_root=Path(runtime_root).as_posix(),
            spawn_id=spawn_id,
            spawn_status=spawn_status,
            spawn_agent=spawn_agent,
            spawn_model=spawn_model,
            spawn_duration_secs=spawn_duration_secs,
            spawn_cost_usd=spawn_cost_usd,
            spawn_error=spawn_error,
            work_id=work_id,
            work_dir=Path(work_dir).as_posix() if work_dir is not None else None,
        )

    @classmethod
    def from_authority(
        cls,
        *,
        authority: object,
        event_name: HookEventName,
        event_id: UUID,
        timestamp: str,
        spawn_id: str | None = None,
        spawn_status: SpawnStatus | None = None,
        spawn_agent: str | None = None,
        spawn_model: str | None = None,
        spawn_duration_secs: float | None = None,
        spawn_cost_usd: float | None = None,
        spawn_error: str | None = None,
        work_id: str | None = None,
        work_dir: str | Path | None = None,
    ) -> HookContext:
        project_root = getattr(authority, "project_root", None)
        runtime_root = getattr(authority, "runtime_root", None)
        if project_root is None:
            raise ValueError("Hook authority is missing project_root.")
        if runtime_root is None:
            raise ValueError("Hook authority is missing runtime_root.")
        return cls.from_roots(
            project_root=project_root,
            runtime_root=runtime_root,
            event_name=event_name,
            event_id=event_id,
            timestamp=timestamp,
            spawn_id=spawn_id,
            spawn_status=spawn_status,
            spawn_agent=spawn_agent,
            spawn_model=spawn_model,
            spawn_duration_secs=spawn_duration_secs,
            spawn_cost_usd=spawn_cost_usd,
            spawn_error=spawn_error,
            work_id=work_id,
            work_dir=work_dir,
        )

    def to_env(self) -> dict[str, str]:
        """Convert context to MERIDIAN_* environment variables."""

        env: dict[str, str | None] = {
            "MERIDIAN_HOOK_EVENT": self.event_name,
            "MERIDIAN_HOOK_EVENT_ID": str(self.event_id),
            "MERIDIAN_HOOK_TIMESTAMP": self.timestamp,
            "MERIDIAN_HOOK_SCHEMA_VERSION": str(self.schema_version),
            "MERIDIAN_PROJECT_DIR": self.project_root,
            "MERIDIAN_RUNTIME_DIR": self.runtime_root,
            "MERIDIAN_SPAWN_ID": self.spawn_id,
            "MERIDIAN_SPAWN_STATUS": self.spawn_status,
            "MERIDIAN_SPAWN_AGENT": self.spawn_agent,
            "MERIDIAN_SPAWN_MODEL": self.spawn_model,
            "MERIDIAN_SPAWN_DURATION_SECS": (
                None if self.spawn_duration_secs is None else str(self.spawn_duration_secs)
            ),
            "MERIDIAN_SPAWN_COST_USD": (
                None if self.spawn_cost_usd is None else str(self.spawn_cost_usd)
            ),
            "MERIDIAN_SPAWN_ERROR": self.spawn_error,
            "MERIDIAN_ACTIVE_WORK_ID": self.work_id,
            "MERIDIAN_ACTIVE_WORK_DIR": self.work_dir,
        }
        return {key: value for key, value in env.items() if value is not None}

    def to_json(self) -> str:
        """Convert context to JSON for stdin transport."""

        payload = {
            "schema_version": self.schema_version,
            "event_name": self.event_name,
            "event_id": str(self.event_id),
            "timestamp": self.timestamp,
            "project_root": self.project_root,
            "runtime_root": self.runtime_root,
            "spawn": {
                "id": self.spawn_id,
                "status": self.spawn_status,
                "agent": self.spawn_agent,
                "model": self.spawn_model,
                "duration_secs": self.spawn_duration_secs,
                "cost_usd": self.spawn_cost_usd,
                "error": self.spawn_error,
            }
            if self.spawn_id
            else None,
            "work": {
                "id": self.work_id,
                "dir": self.work_dir,
            }
            if self.work_id
            else None,
        }
        return json.dumps(payload)


@dataclass
class HookResult:
    """Plugin-facing result for one hook execution."""

    hook_name: str
    event: HookEventName
    outcome: HookOutcome
    success: bool
    skipped: bool = False
    skip_reason: str | None = None
    error: str | None = None
    exit_code: int | None = None
    duration_ms: int = 0
    stdout: str | None = None
    stderr: str | None = None


__all__ = [
    "FailurePolicy",
    "Hook",
    "HookContext",
    "HookEventName",
    "HookOutcome",
    "HookResult",
]
