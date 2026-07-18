"""Hook contracts shared across registration, dispatch, and execution."""

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

HookEventClass = Literal["observe", "post", "gate"]

SpawnStatus = Literal["success", "failure", "cancelled", "timeout", "skipped"]
FailurePolicy = Literal["fail", "warn", "ignore"]
HookOutcome = Literal["success", "failure", "timeout", "skipped"]

EVENT_CLASS: dict[HookEventName, HookEventClass] = {
    "spawn.created": "observe",
    "spawn.running": "observe",
    "spawn.start": "observe",
    "spawn.finalized": "post",
    "work.start": "observe",
    "work.started": "observe",
    "work.done": "post",
}

DEFAULT_TIMEOUTS: dict[HookEventClass, int] = {
    "observe": 30,
    "post": 60,
    "gate": 1,
}

DEFAULT_FAILURE_POLICY: dict[HookEventClass, FailurePolicy] = {
    "observe": "warn",
    "post": "warn",
    "gate": "fail",
}

HOOK_CONTEXT_SCHEMA_VERSION = 1


def _default_options() -> dict[str, object]:
    return {}


@dataclass(frozen=True)
class HookWhen:
    """Conditional execution filters."""

    status: tuple[SpawnStatus, ...] | None = None
    agent: str | None = None


@dataclass(frozen=True)
class Hook:
    """Registered hook configuration."""

    name: str
    event: HookEventName
    source: str
    command: str | None = None
    builtin: str | None = None
    timeout_secs: int | None = None
    interval: str | None = None
    enabled: bool = True
    priority: int = 0
    failure_policy: FailurePolicy | None = None
    require_serial: bool = False
    when: HookWhen | None = None
    exclude: tuple[str, ...] = ()
    options: Mapping[str, object] = field(default_factory=_default_options)
    auto_registered: bool = False
    # Legacy field kept for backward compatibility.
    # Builtins should prefer config.options.
    remote: str | None = None


@dataclass(frozen=True)
class HookContext:
    """Structured event context passed to hooks."""

    event_name: HookEventName
    event_id: UUID
    timestamp: str
    project_root: str
    runtime_root: str
    schema_version: int = HOOK_CONTEXT_SCHEMA_VERSION

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

        from meridian.env_registry import HOOK_PAYLOAD_ENV_KEYS

        values: dict[str, str | None] = {
            "event_name": self.event_name,
            "event_id": str(self.event_id),
            "timestamp": self.timestamp,
            "schema_version": str(self.schema_version),
            "project_root": self.project_root,
            "spawn_id": self.spawn_id,
            "spawn_status": self.spawn_status,
            "spawn_agent": self.spawn_agent,
            "spawn_model": self.spawn_model,
            "spawn_duration_secs": (
                None if self.spawn_duration_secs is None else str(self.spawn_duration_secs)
            ),
            "spawn_cost_usd": (
                None if self.spawn_cost_usd is None else str(self.spawn_cost_usd)
            ),
            "spawn_error": self.spawn_error,
            "work_id": self.work_id,
            "work_dir": self.work_dir,
        }
        return {
            HOOK_PAYLOAD_ENV_KEYS[field]: value
            for field, value in values.items()
            if value is not None
        }

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
            if self.work_id or self.work_dir
            else None,
        }
        return json.dumps(payload)


@dataclass
class HookResult:
    """Result for one hook execution."""

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
