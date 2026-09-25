"""Shared launch artifact helpers."""

from __future__ import annotations

import json
import os
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import structlog

from meridian.lib.core.clock import Clock
from meridian.lib.core.native_identity import NativeIdentityError
from meridian.lib.core.types import ArtifactKey, HarnessId, SpawnId
from meridian.lib.launch.composition import (
    ProjectionChannels,
    ReferenceRouting,
    build_inline_file_contributions,
    build_reference_routing,
)
from meridian.lib.state.artifact_store import ArtifactStore
from meridian.lib.state.atomic import append_text_line, atomic_write_text
from meridian.lib.state.spawn_aggregate import mutate_published_spawn_artifact

if TYPE_CHECKING:
    from meridian.lib.launch.context import LaunchContext


logger = structlog.get_logger(__name__)


def append_runner_lifecycle_event(
    runtime_root: Path,
    spawn_id: SpawnId,
    path: Path,
    *,
    clock: Clock,
    event: str,
    phase: str,
    **details: object,
) -> None:
    """Best-effort append of runner-owned crash diagnostics."""

    payload = {
        "event": event,
        "timestamp": clock.utc_now_iso(),
        "pid": os.getpid(),
        "phase": phase,
        **details,
    }
    try:
        mutate_published_spawn_artifact(
            runtime_root,
            spawn_id,
            lambda: append_text_line(
                path,
                json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
            ),
        )
    except Exception:
        logger.warning("Failed to append runner lifecycle evidence.", exc_info=True)


@dataclass(frozen=True)
class LifecycleLog:
    """Destination and clock for runner-owned lifecycle evidence."""

    runtime_root: Path
    spawn_id: SpawnId
    path: Path
    clock: Clock

    def __call__(self, *, event: str, phase: str, **details: object) -> None:
        append_runner_lifecycle_event(
            self.runtime_root, self.spawn_id, self.path, clock=self.clock,
            event=event, phase=phase, **details,
        )


def record_identity_failure(
    error: NativeIdentityError, *, lifecycle: LifecycleLog, phase: str,
) -> None:
    """Write the same typed refusal payload at every runner boundary."""
    lifecycle(event=error.failure_code, phase=phase, **error.lifecycle_fields())


ProjectionSurface = Literal["primary", "spawn"]


def read_artifact_text(artifacts: ArtifactStore, spawn_id: SpawnId, name: str) -> str:
    key = ArtifactKey(f"{spawn_id}/{name}")
    if not artifacts.exists(key):
        return ""
    return artifacts.get(key).decode("utf-8", errors="ignore")


def _resolve_reference_routing(launch_context: LaunchContext) -> tuple[ReferenceRouting, ...]:
    projected = launch_context.projected_content
    if projected is not None:
        return projected.reference_routing

    reference_items = launch_context.binding.run_params.reference_items
    if not reference_items:
        return ()
    return build_reference_routing(reference_items)


def _fallback_projection_channels(
    *,
    launch_context: LaunchContext,
    reference_routing: tuple[ReferenceRouting, ...],
) -> ProjectionChannels:
    harness_id = launch_context.harness.id
    has_append_system_prompt = bool(
        (launch_context.binding.run_params.appended_system_prompt or "").strip()
    )
    has_native_injection = any(route.routing == "native-injection" for route in reference_routing)

    if harness_id == HarnessId.CLAUDE:
        if has_append_system_prompt:
            return ProjectionChannels(
                system_instruction="append-system-prompt",
                user_task_prompt="inline",
                task_context="inline",
            )
        return ProjectionChannels(
            system_instruction="inline",
            user_task_prompt="inline",
            task_context="inline",
        )

    return ProjectionChannels(
        system_instruction="inline",
        user_task_prompt="inline",
        task_context="native-injection" if has_native_injection else "inline",
    )


def _resolve_projection_channels(
    *,
    launch_context: LaunchContext,
    reference_routing: tuple[ReferenceRouting, ...],
) -> ProjectionChannels:
    projected = launch_context.projected_content
    if projected is not None:
        return projected.channels
    return _fallback_projection_channels(
        launch_context=launch_context,
        reference_routing=reference_routing,
    )


def _write_inline_file_reference_byte_accounting(
    *,
    log_dir: Path,
    launch_context: LaunchContext,
    reference_routing: tuple[ReferenceRouting, ...],
) -> None:
    reference_items = launch_context.binding.run_params.reference_items
    if not reference_items:
        return
    contributions = build_inline_file_contributions(reference_items, reference_routing)
    if not contributions:
        return

    payload = {
        "total_inline_file_bytes": sum(
            contribution.byte_count for contribution in contributions
        ),
        "inline_file_references_by_size": [
            contribution.to_dict() for contribution in contributions
        ],
    }
    atomic_write_text(
        log_dir / "inline-file-reference-bytes.json",
        json.dumps(payload, indent=2) + "\n",
    )


def write_projection_artifacts(
    *,
    log_dir: Path,
    launch_context: LaunchContext,
    surface: ProjectionSurface,
) -> None:
    """Write launch observability artifacts for one prepared context."""

    projected = launch_context.projected_content
    if projected is not None:
        system_prompt = projected.system_prompt.strip()
        starting_prompt = projected.user_turn_content.strip()
    else:
        system_prompt = (launch_context.binding.run_params.appended_system_prompt or "").strip()
        user_turn = (launch_context.binding.run_params.user_turn_content or "").strip()
        starting_prompt = user_turn or launch_context.request.prompt.strip()

    if system_prompt:
        atomic_write_text(log_dir / "system-prompt.md", system_prompt)
    if starting_prompt:
        atomic_write_text(log_dir / "starting-prompt.md", starting_prompt)
    with suppress(FileNotFoundError):
        (log_dir / "prompt.md").unlink()

    reference_routing = _resolve_reference_routing(launch_context)
    if reference_routing:
        atomic_write_text(
            log_dir / "references.json",
            json.dumps([route.to_dict() for route in reference_routing], indent=2) + "\n",
        )
    _write_inline_file_reference_byte_accounting(
        log_dir=log_dir,
        launch_context=launch_context,
        reference_routing=reference_routing,
    )

    channels = _resolve_projection_channels(
        launch_context=launch_context,
        reference_routing=reference_routing,
    )
    manifest_payload = {
        "harness": launch_context.harness.id.value,
        "surface": surface,
        "channels": channels.to_dict(),
    }
    atomic_write_text(
        log_dir / "projection-manifest.json",
        json.dumps(manifest_payload, indent=2) + "\n",
    )
