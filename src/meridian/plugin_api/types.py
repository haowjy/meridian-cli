"""Stable hook contract types owned by the plugin API."""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import UUID

HookEventName = Literal[
    "spawn.created",
    "spawn.started",
    "spawn.finished",
    "work.created",
    "work.activated",
    "work.done",
    "notify.your_turn",
    "notify.cache_expiring",
    "notify.needs_permission",
    "notify.manual",
]

# Events that carry a `HookContext.notification`.
NOTIFICATION_EVENTS: tuple[HookEventName, ...] = (
    "notify.your_turn",
    "notify.cache_expiring",
    "notify.needs_permission",
    "notify.manual",
)
HOOK_CONTEXT_SCHEMA_VERSION = 3

HookOutcome = Literal["success", "failure", "timeout", "skipped"]
SpawnKind = Literal["child", "primary", "streaming"]
SpawnStatus = Literal["success", "failure", "cancelled", "timeout"]


def _default_options() -> dict[str, object]:
    return {}


def _empty_label() -> dict[str, str | None]:
    return {}


def _empty_details() -> dict[str, object]:
    return {}


@dataclass(frozen=True)
class Notification:
    """One message for the human, as its producer rendered it."""

    title: str
    body: str
    # 1 (min) to 5 (urgent), ntfy's scale.
    priority: int = 3
    # Whether the producer wants email channels to carry it.
    email: bool = True
    # Where it came from: tmux_session, project, work_id, agent, harness, headline.
    label: Mapping[str, str | None] = field(default_factory=_empty_label)
    # Absolute paths of files to send with it, checked readable when it was made.
    attachments: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("title must not be empty")
        if any(unicodedata.category(character) == "Cc" for character in self.title):
            raise ValueError("title must not contain control characters")
        if not 1 <= self.priority <= 5:
            raise ValueError(f"priority must be between 1 and 5, got {self.priority}")

    def to_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "body": self.body,
            "priority": self.priority,
            "email": self.email,
            "label": dict(self.label),
            "attachments": list(self.attachments),
        }


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
    remote: str | None = None


@dataclass(frozen=True)
class HookContext:
    """Plugin-facing structured hook context."""

    event_name: HookEventName
    event_id: UUID
    timestamp: str
    project_root: str
    runtime_root: str | None
    schema_version: int = HOOK_CONTEXT_SCHEMA_VERSION

    spawn_id: str | None = None
    spawn_status: SpawnStatus | None = None
    spawn_agent: str | None = None
    spawn_kind: SpawnKind | None = None
    spawn_model: str | None = None
    spawn_duration_secs: float | None = None
    spawn_cost_usd: float | None = None
    spawn_error: str | None = None

    work_id: str | None = None
    work_dir: str | None = None

    notification: Notification | None = None

    @classmethod
    def from_roots(
        cls,
        *,
        project_root: str | Path,
        runtime_root: str | Path | None,
        event_name: HookEventName,
        event_id: UUID,
        timestamp: str,
        spawn_id: str | None = None,
        spawn_status: SpawnStatus | None = None,
        spawn_agent: str | None = None,
        spawn_kind: SpawnKind | None = None,
        spawn_model: str | None = None,
        spawn_duration_secs: float | None = None,
        spawn_cost_usd: float | None = None,
        spawn_error: str | None = None,
        work_id: str | None = None,
        work_dir: str | Path | None = None,
        notification: Notification | None = None,
    ) -> HookContext:
        return cls(
            event_name=event_name,
            event_id=event_id,
            timestamp=timestamp,
            project_root=Path(project_root).as_posix(),
            runtime_root=Path(runtime_root).as_posix() if runtime_root is not None else None,
            spawn_id=spawn_id,
            spawn_status=spawn_status,
            spawn_agent=spawn_agent,
            spawn_kind=spawn_kind,
            spawn_model=spawn_model,
            spawn_duration_secs=spawn_duration_secs,
            spawn_cost_usd=spawn_cost_usd,
            spawn_error=spawn_error,
            work_id=work_id,
            work_dir=Path(work_dir).as_posix() if work_dir is not None else None,
            notification=notification,
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
            "spawn_kind": self.spawn_kind,
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
            "notification_title": self.notification.title if self.notification else None,
            "notification_body": self.notification.body if self.notification else None,
            "notification_priority": (
                str(self.notification.priority) if self.notification else None
            ),
        }
        return {
            HOOK_PAYLOAD_ENV_KEYS[field]: value
            for field, value in values.items()
            if value is not None
        }

    def to_json(self) -> str:
        """Convert context to JSON for stdin transport."""

        has_spawn_context = any(
            value is not None
            for value in (
                self.spawn_id,
                self.spawn_status,
                self.spawn_agent,
                self.spawn_kind,
                self.spawn_model,
                self.spawn_duration_secs,
                self.spawn_cost_usd,
                self.spawn_error,
            )
        )
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
                "kind": self.spawn_kind,
                "model": self.spawn_model,
                "duration_secs": self.spawn_duration_secs,
                "cost_usd": self.spawn_cost_usd,
                "error": self.spawn_error,
            }
            if has_spawn_context
            else None,
            "work": {
                "id": self.work_id,
                "dir": self.work_dir,
            }
            if self.work_id or self.work_dir
            else None,
            "notification": self.notification.to_dict() if self.notification else None,
        }
        return json.dumps(payload)


@dataclass
class HookResult:
    """Plugin-facing result for one hook execution.

    Builtin hook implementations may set ``details`` to JSON-serializable
    structured data. Builtins document the keys they produce and the surfaces
    that project them.
    """

    hook_name: str
    event: HookEventName
    outcome: HookOutcome
    success: bool
    skipped: bool = False
    skip_reason: str | None = None
    warning: str | None = None
    error: str | None = None
    exit_code: int | None = None
    duration_ms: int = 0
    stdout: str | None = None
    stderr: str | None = None
    details: Mapping[str, object] = field(default_factory=_empty_details)


__all__ = [
    "HOOK_CONTEXT_SCHEMA_VERSION",
    "NOTIFICATION_EVENTS",
    "Hook",
    "HookContext",
    "HookEventName",
    "HookOutcome",
    "HookResult",
    "Notification",
    "SpawnKind",
    "SpawnStatus",
]
