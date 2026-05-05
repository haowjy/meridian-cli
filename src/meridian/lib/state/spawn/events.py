"""Pure spawn event reduction logic."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from meridian.lib.core.spawn_lifecycle import (
    TERMINAL_SPAWN_STATUSES as _TERMINAL_SPAWN_STATUSES,
)

if TYPE_CHECKING:
    from meridian.lib.state.spawn_store import SpawnEvent, SpawnRecord


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
    # Intentional seam boundary: this reducer was extracted for tests, but event
    # model ownership still lives in spawn_store, so runtime type imports remain.
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

        if isinstance(event, spawn_store.SpawnStartEvent):
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

        if isinstance(event, spawn_store.SpawnUpdateEvent):
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

        if isinstance(event, spawn_store.SpawnExitedEvent):
            records[spawn_id] = current.model_copy(
                update={
                    "exited_at": event.exited_at
                    if event.exited_at is not None
                    else current.exited_at,
                    "process_exit_code": event.exit_code,
                }
            )
            continue

        if not isinstance(event, spawn_store.SpawnFinalizeEvent):
            continue

        finalize_event = event
        incoming_origin = (
            finalize_event.origin if finalize_event.origin is not None else "runner"
        )
        incoming_authoritative = incoming_origin in spawn_store.AUTHORITATIVE_ORIGINS
        already_terminal = current.status in _TERMINAL_SPAWN_STATUSES
        replace_terminal = (
            not already_terminal
            or (current.terminal_origin == "reconciler" and incoming_authoritative)
        )
        if not replace_terminal:
            resolved_status = current.status
            resolved_exit_code = current.exit_code
            resolved_error = current.error
            resolved_terminal_origin = current.terminal_origin
        else:
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


__all__ = ["reduce_events"]
