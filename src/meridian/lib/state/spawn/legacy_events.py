"""Legacy spawn event models and reduction logic.

This module is the transition seam for the v1 JSONL spawn event format while
spawn state v2 is introduced. Public compatibility re-exports remain in
``meridian.lib.state.spawn_store`` and ``meridian.lib.state.spawn.events``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, ValidationError

from meridian.lib.core.domain import SpawnStatus
from meridian.lib.core.spawn_lifecycle import (
    TERMINAL_SPAWN_STATUSES as _TERMINAL_SPAWN_STATUSES,
)
from meridian.lib.state.spawn.terminal_policy import decide_terminal_write

if TYPE_CHECKING:
    from meridian.lib.state.spawn_store import SpawnRecord


LaunchMode = Literal["background", "foreground", "app"]
BACKGROUND_LAUNCH_MODE: LaunchMode = "background"
FOREGROUND_LAUNCH_MODE: LaunchMode = "foreground"
APP_LAUNCH_MODE: LaunchMode = "app"
SpawnOrigin = Literal["runner", "launcher", "launch_failure", "cancel", "reconciler"]
_LAUNCH_MODE_VALUES: frozenset[LaunchMode] = frozenset(
    (BACKGROUND_LAUNCH_MODE, FOREGROUND_LAUNCH_MODE, APP_LAUNCH_MODE)
)

_AUTHORITATIVE_ORIGIN_VALUES: tuple[SpawnOrigin, ...] = (
    "runner",
    "launcher",
    "launch_failure",
    "cancel",
)
AUTHORITATIVE_ORIGINS: frozenset[SpawnOrigin] = frozenset(_AUTHORITATIVE_ORIGIN_VALUES)


class SpawnStartEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    v: int = 1
    event: Literal["start"] = "start"
    id: str = ""
    chat_id: str | None = None
    parent_id: str | None = None
    model: str | None = None
    agent: str | None = None
    agent_path: str | None = None
    skills: tuple[str, ...] = ()
    skill_paths: tuple[str, ...] = ()
    harness: str | None = None
    kind: str | None = None
    desc: str | None = None
    work_id: str | None = None
    harness_session_id: str | None = None
    execution_cwd: str | None = None
    launch_mode: LaunchMode | None = None
    worker_pid: int | None = None
    runner_pid: int | None = None
    status: SpawnStatus = "running"
    prompt: str | None = None
    started_at: str | None = None


class SpawnUpdateEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    v: int = 1
    event: Literal["update"] = "update"
    id: str = ""
    status: SpawnStatus | None = None
    launch_mode: LaunchMode | None = None
    worker_pid: int | None = None
    runner_pid: int | None = None
    harness_session_id: str | None = None
    execution_cwd: str | None = None
    claude_config_dir: str | None = None
    error: str | None = None
    desc: str | None = None
    work_id: str | None = None


class SpawnExitedEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    v: int = 1
    event: Literal["exited"] = "exited"
    id: str = ""
    exit_code: int = 0
    exited_at: str | None = None


class SpawnFinalizeEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    v: int = 1
    event: Literal["finalize"] = "finalize"
    id: str = ""
    status: SpawnStatus | None = None
    exit_code: int | None = None
    finished_at: str | None = None
    duration_secs: float | None = None
    total_cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_is_estimate: bool = False
    error: str | None = None
    origin: SpawnOrigin | None = None


type SpawnEvent = SpawnStartEvent | SpawnUpdateEvent | SpawnExitedEvent | SpawnFinalizeEvent


def _coerce_launch_mode(value: object) -> object:
    if not isinstance(value, str):
        return value
    normalized = value.strip().lower()
    if normalized in _LAUNCH_MODE_VALUES:
        return normalized
    return value


def _parse_event(payload: dict[str, Any]) -> SpawnEvent | None:
    resolved_payload = payload
    if "launch_mode" in payload:
        coerced_launch_mode = _coerce_launch_mode(payload.get("launch_mode"))
        if coerced_launch_mode != payload.get("launch_mode"):
            resolved_payload = dict(payload)
            resolved_payload["launch_mode"] = coerced_launch_mode

    event_type = resolved_payload.get("event")
    try:
        if event_type == "start":
            return SpawnStartEvent.model_validate(resolved_payload)
        if event_type == "update":
            return SpawnUpdateEvent.model_validate(resolved_payload)
        if event_type == "exited":
            return SpawnExitedEvent.model_validate(resolved_payload)
        if event_type == "finalize":
            return SpawnFinalizeEvent.model_validate(resolved_payload)
    except ValidationError:
        return None
    return None


parse_event = _parse_event


def _empty_record(
    spawn_id: str,
    *,
    spawn_record_type: type[SpawnRecord],
) -> SpawnRecord:
    return spawn_record_type(
        id=spawn_id,
        chat_id=None,
        parent_id=None,
        model=None,
        agent=None,
        agent_path=None,
        skills=(),
        skill_paths=(),
        harness=None,
        kind="child",
        desc=None,
        work_id=None,
        harness_session_id=None,
        execution_cwd=None,
        claude_config_dir=None,
        launch_mode=None,
        worker_pid=None,
        runner_pid=None,
        status="unknown",
        prompt=None,
        started_at=None,
        exited_at=None,
        process_exit_code=None,
        finished_at=None,
        exit_code=None,
        duration_secs=None,
        total_cost_usd=None,
        input_tokens=None,
        output_tokens=None,
        cache_read_input_tokens=None,
        cache_creation_input_tokens=None,
        reasoning_tokens=None,
        cost_is_estimate=False,
        error=None,
        terminal_origin=None,
    )


def _normalized_work_id(work_id: str | None) -> str | None:
    if work_id is None:
        return None
    normalized = work_id.strip()
    return normalized or None


def reduce_events(events: list[SpawnEvent]) -> dict[str, SpawnRecord]:
    # Runtime import keeps SpawnRecord ownership in spawn_store during the v2
    # transition while legacy event/reducer ownership moves behind this seam.
    from meridian.lib.state import spawn_store

    records: dict[str, SpawnRecord] = {}

    for event in events:
        event = cast("Any", event)
        spawn_id = getattr(event, "id", None)
        if not spawn_id:
            continue
        current = records.get(
            spawn_id,
            _empty_record(spawn_id, spawn_record_type=spawn_store.SpawnRecord),
        )

        if isinstance(event, SpawnStartEvent):
            chat_id = getattr(event, "chat_id", None)
            parent_id = getattr(event, "parent_id", None)
            model = getattr(event, "model", None)
            agent = getattr(event, "agent", None)
            agent_path = getattr(event, "agent_path", None)
            skills = getattr(event, "skills", None)
            skill_paths = getattr(event, "skill_paths", None)
            harness = getattr(event, "harness", None)
            kind = getattr(event, "kind", None)
            desc = getattr(event, "desc", None)
            work_id = getattr(event, "work_id", None)
            harness_session_id = getattr(event, "harness_session_id", None)
            execution_cwd = getattr(event, "execution_cwd", None)
            claude_config_dir = getattr(event, "claude_config_dir", None)
            launch_mode = getattr(event, "launch_mode", None)
            worker_pid = getattr(event, "worker_pid", None)
            runner_pid = getattr(event, "runner_pid", None)
            status = getattr(event, "status", current.status)
            prompt = getattr(event, "prompt", None)
            started_at = getattr(event, "started_at", None)
            records[spawn_id] = current.model_copy(
                update={
                    "chat_id": chat_id if chat_id is not None else current.chat_id,
                    "parent_id": parent_id if parent_id is not None else current.parent_id,
                    "model": model if model is not None else current.model,
                    "agent": agent if agent is not None else current.agent,
                    "agent_path": agent_path if agent_path is not None else current.agent_path,
                    "skills": skills if skills else current.skills,
                    "skill_paths": skill_paths if skill_paths else current.skill_paths,
                    "harness": harness if harness is not None else current.harness,
                    "kind": kind if kind is not None else current.kind,
                    "desc": desc if desc is not None else current.desc,
                    "work_id": (
                        _normalized_work_id(work_id) if work_id is not None else current.work_id
                    ),
                    "harness_session_id": (
                        harness_session_id
                        if harness_session_id is not None
                        else current.harness_session_id
                    ),
                    "execution_cwd": (
                        execution_cwd if execution_cwd is not None else current.execution_cwd
                    ),
                    "claude_config_dir": (
                        claude_config_dir
                        if claude_config_dir is not None
                        else current.claude_config_dir
                    ),
                    "launch_mode": launch_mode if launch_mode is not None else current.launch_mode,
                    "worker_pid": worker_pid if worker_pid is not None else current.worker_pid,
                    "runner_pid": runner_pid if runner_pid is not None else current.runner_pid,
                    "status": status,
                    "prompt": prompt if prompt is not None else current.prompt,
                    "started_at": started_at if started_at is not None else current.started_at,
                }
            )
            continue

        if isinstance(event, SpawnUpdateEvent):
            resolved_status = current.status
            if event.status is not None and current.status not in _TERMINAL_SPAWN_STATUSES:
                resolved_status = event.status
            records[spawn_id] = current.model_copy(
                update={
                    "status": resolved_status,
                    "launch_mode": (
                        event.launch_mode if event.launch_mode is not None else current.launch_mode
                    ),
                    "worker_pid": (
                        event.worker_pid if event.worker_pid is not None else current.worker_pid
                    ),
                    "runner_pid": (
                        event.runner_pid if event.runner_pid is not None else current.runner_pid
                    ),
                    "harness_session_id": (
                        event.harness_session_id
                        if event.harness_session_id is not None
                        else current.harness_session_id
                    ),
                    "execution_cwd": (
                        event.execution_cwd
                        if event.execution_cwd is not None
                        else current.execution_cwd
                    ),
                    "claude_config_dir": (
                        event.claude_config_dir
                        if event.claude_config_dir is not None
                        else current.claude_config_dir
                    ),
                    "error": event.error if event.error is not None else current.error,
                    "desc": event.desc if event.desc is not None else current.desc,
                    "work_id": (
                        _normalized_work_id(event.work_id)
                        if event.work_id is not None
                        else current.work_id
                    ),
                }
            )
            continue

        if isinstance(event, SpawnExitedEvent):
            records[spawn_id] = current.model_copy(
                update={
                    "exited_at": event.exited_at
                    if event.exited_at is not None
                    else current.exited_at,
                    "process_exit_code": event.exit_code,
                }
            )
            continue

        if not isinstance(event, SpawnFinalizeEvent):
            continue

        finalize_event = event
        incoming_origin = (
            finalize_event.origin if finalize_event.origin is not None else "runner"
        )
        terminal_decision = decide_terminal_write(
            current_status=current.status,
            current_terminal_origin=current.terminal_origin,
            incoming_origin=incoming_origin,
        )
        if terminal_decision.disposition == "reject":
            continue
        event_status = (
            finalize_event.status
            if finalize_event.status is not None
            else current.status
        )
        resolved_status = event_status
        resolved_exit_code = (
            finalize_event.exit_code
            if finalize_event.exit_code is not None
            else current.exit_code
        )
        resolved_error = finalize_event.error
        resolved_terminal_origin = incoming_origin
        records[spawn_id] = current.model_copy(
            update={
                "status": resolved_status,
                "finished_at": finalize_event.finished_at
                if finalize_event.finished_at is not None
                else current.finished_at,
                "exit_code": resolved_exit_code,
                "duration_secs": (
                    finalize_event.duration_secs
                    if finalize_event.duration_secs is not None
                    else current.duration_secs
                ),
                "total_cost_usd": (
                    finalize_event.total_cost_usd
                    if finalize_event.total_cost_usd is not None
                    else current.total_cost_usd
                ),
                "input_tokens": (
                    finalize_event.input_tokens
                    if finalize_event.input_tokens is not None
                    else current.input_tokens
                ),
                "output_tokens": (
                    finalize_event.output_tokens
                    if finalize_event.output_tokens is not None
                    else current.output_tokens
                ),
                "cache_read_input_tokens": (
                    finalize_event.cache_read_input_tokens
                    if finalize_event.cache_read_input_tokens is not None
                    else current.cache_read_input_tokens
                ),
                "cache_creation_input_tokens": (
                    finalize_event.cache_creation_input_tokens
                    if finalize_event.cache_creation_input_tokens is not None
                    else current.cache_creation_input_tokens
                ),
                "reasoning_tokens": (
                    finalize_event.reasoning_tokens
                    if finalize_event.reasoning_tokens is not None
                    else current.reasoning_tokens
                ),
                "cost_is_estimate": finalize_event.cost_is_estimate or current.cost_is_estimate,
                "error": resolved_error,
                "terminal_origin": resolved_terminal_origin,
            }
        )

    return records


__all__ = [
    "APP_LAUNCH_MODE",
    "AUTHORITATIVE_ORIGINS",
    "BACKGROUND_LAUNCH_MODE",
    "FOREGROUND_LAUNCH_MODE",
    "_AUTHORITATIVE_ORIGIN_VALUES",
    "_LAUNCH_MODE_VALUES",
    "LaunchMode",
    "SpawnEvent",
    "SpawnExitedEvent",
    "SpawnFinalizeEvent",
    "SpawnOrigin",
    "SpawnStartEvent",
    "SpawnUpdateEvent",
    "_coerce_launch_mode",
    "_parse_event",
    "parse_event",
    "reduce_events",
]
