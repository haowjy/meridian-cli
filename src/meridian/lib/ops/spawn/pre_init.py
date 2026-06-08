"""Canonical pre-initialization failure boundary for spawn.create."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

import structlog

from meridian.lib.launch.request import SpawnRequest

from .models import SpawnActionOutput, SpawnCreateInput

_T = TypeVar("_T")
logger = structlog.get_logger(__name__)


class PreInitFailure(Exception):
    """Expected spawn setup failure safe to present as ``pre_init_failed``."""


EXPECTED_PRE_INIT_EXCEPTIONS = (
    ValueError,
    FileNotFoundError,
    PermissionError,
)


def _payload_task_cwd_source(payload: SpawnCreateInput) -> str | None:
    if payload.task_dir:
        return "explicit-task-dir"
    if payload.work.strip():
        return "explicit-work-authority-root"
    return None


def _pre_init_output(
    *,
    payload: SpawnCreateInput,
    message: str,
    error: str,
    request: SpawnRequest | None = None,
) -> SpawnActionOutput:
    return SpawnActionOutput(
        command="spawn.create",
        status="failed",
        message=message,
        error=error,
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


def pre_init_failed_output(
    *,
    payload: SpawnCreateInput,
    exc: Exception,
    request: SpawnRequest | None = None,
) -> SpawnActionOutput:
    return _pre_init_output(
        payload=payload,
        request=request,
        message=f"Spawn setup failed before initialization: {exc}",
        error="pre_init_failed",
    )


def unexpected_pre_init_failed_output(
    *,
    payload: SpawnCreateInput,
    exc: Exception,
    request: SpawnRequest | None = None,
) -> SpawnActionOutput:
    return _pre_init_output(
        payload=payload,
        request=request,
        message=(
            "Unexpected spawn setup error before initialization: "
            f"{type(exc).__name__}: {exc}"
        ),
        error="pre_init_unexpected_error",
    )


def run_pre_init_boundary(
    *,
    payload: SpawnCreateInput | Callable[[], SpawnCreateInput],
    operation: Callable[[], _T],
    request: SpawnRequest | None = None,
) -> _T | SpawnActionOutput:
    try:
        return operation()
    except PreInitFailure as exc:
        failure_payload = payload() if callable(payload) else payload
        return pre_init_failed_output(payload=failure_payload, request=request, exc=exc)
    except Exception as exc:
        logger.error(
            "spawn_pre_init_unexpected_exception",
            error_type=type(exc).__name__,
            exc_info=True,
        )
        failure_payload = payload() if callable(payload) else payload
        return unexpected_pre_init_failed_output(payload=failure_payload, request=request, exc=exc)


__all__ = [
    "EXPECTED_PRE_INIT_EXCEPTIONS",
    "PreInitFailure",
    "pre_init_failed_output",
    "run_pre_init_boundary",
    "unexpected_pre_init_failed_output",
]
