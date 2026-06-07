"""Canonical pre-initialization failure boundary for spawn.create."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from meridian.lib.launch.request import SpawnRequest

from .models import SpawnActionOutput, SpawnCreateInput

_T = TypeVar("_T")


def _payload_task_cwd_source(payload: SpawnCreateInput) -> str | None:
    if payload.task_dir:
        return "explicit-task-dir"
    if payload.work.strip():
        return "explicit-work-authority-root"
    return None


def pre_init_failed_output(
    *,
    payload: SpawnCreateInput,
    exc: Exception,
    request: SpawnRequest | None = None,
) -> SpawnActionOutput:
    return SpawnActionOutput(
        command="spawn.create",
        status="failed",
        message=f"Spawn setup failed before initialization: {exc}",
        error="pre_init_failed",
        model=(request.model if request is not None else payload.model) or "",
        harness_id=(request.harness if request is not None else payload.harness) or "",
        warning=request.warning if request is not None else None,
        agent=request.agent if request is not None else payload.agent,
        skills=() if request is not None else payload.skills,
        reference_files=request.reference_files if request is not None else payload.files,
        template_vars=request.template_vars if request is not None else {},
        context_from_resolved=request.context_from if request is not None else payload.context_from,
        exit_code=1,
        authority_root=(
            (request.authority_root or None) if request is not None else payload.project_root
        ),
        task_cwd=(request.task_cwd or None) if request is not None else payload.task_dir,
        reference_anchor=(
            (request.reference_anchor or request.task_cwd or None)
            if request is not None
            else (payload.task_dir or payload.project_root)
        ),
        task_cwd_source=(
            request.task_cwd_source if request is not None else _payload_task_cwd_source(payload)
        ),
        task_cwd_work_item=(
            request.task_cwd_work_item if request is not None else (payload.work.strip() or None)
        ),
    )


def run_pre_init_boundary(
    *,
    payload: SpawnCreateInput | Callable[[], SpawnCreateInput],
    operation: Callable[[], _T],
    request: SpawnRequest | None = None,
) -> _T | SpawnActionOutput:
    try:
        return operation()
    except Exception as exc:
        failure_payload = payload() if callable(payload) else payload
        return pre_init_failed_output(payload=failure_payload, request=request, exc=exc)


__all__ = ["pre_init_failed_output", "run_pre_init_boundary"]
