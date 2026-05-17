"""Pi harness extractor."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from meridian.lib.core.domain import TokenUsage
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.adapter import ArtifactStore
from meridian.lib.harness.common import (
    OUTPUT_FILENAME,
    _coerce_optional_int,  # pyright: ignore[reportPrivateUsage]
    _iter_json_lines_artifact,  # pyright: ignore[reportPrivateUsage]
)
from meridian.lib.harness.connections.base import HarnessEvent
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.state.user_paths import get_user_home

from .base import HarnessExtractor


def _safe_resolve(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except OSError:
        return path.expanduser().absolute()


def _encode_cwd_for_session_dir(cwd: Path) -> str:
    normalized = str(_safe_resolve(cwd)).replace("\\", "/")
    encoded = normalized.replace("/", "--")
    if not encoded.endswith("--"):
        encoded = f"{encoded}--"
    return encoded


def _pi_session_root(launch_env: Mapping[str, str]) -> Path:
    session_dir_override = launch_env.get("PI_CODING_AGENT_SESSION_DIR", "").strip()
    if session_dir_override:
        return Path(session_dir_override).expanduser()

    agent_dir = launch_env.get("PI_CODING_AGENT_DIR", "").strip()
    if agent_dir:
        return Path(agent_dir).expanduser() / "sessions"

    return get_user_home() / "meridian-pi" / "sessions"


def _iter_session_id_candidates_from_artifacts(
    artifacts: ArtifactStore,
    spawn_id: SpawnId,
) -> list[str]:
    payloads = _iter_json_lines_artifact(artifacts, spawn_id, OUTPUT_FILENAME)
    session_ids: list[str] = []
    for payload in payloads:
        if str(payload.get("type", "")).strip().lower() != "session":
            continue
        session_id = payload.get("id")
        if isinstance(session_id, str) and session_id.strip():
            session_ids.append(session_id.strip())
    return session_ids


def _session_id_from_session_files(
    *,
    launch_env: Mapping[str, str],
    child_cwd: Path,
) -> str | None:
    root = _pi_session_root(launch_env)
    if not root.is_dir():
        return None

    session_dir = root / _encode_cwd_for_session_dir(child_cwd)
    if not session_dir.is_dir():
        return None

    candidates: list[tuple[float, Path]] = []
    for session_file in session_dir.glob("*.jsonl"):
        try:
            modified_at = session_file.stat().st_mtime
        except OSError:
            continue
        candidates.append((modified_at, session_file))

    for _, path in sorted(candidates, key=lambda item: item[0], reverse=True):
        try:
            first_line = path.read_text(
                encoding="utf-8",
                errors="ignore",
            ).splitlines()[0]
        except (OSError, IndexError):
            continue
        try:
            payload_obj = json.loads(first_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload_obj, dict):
            continue
        payload = cast("dict[str, object]", payload_obj)
        if str(payload.get("type", "")).strip().lower() != "session":
            continue
        session_id = payload.get("id")
        if isinstance(session_id, str) and session_id.strip():
            return session_id.strip()

    return None


def _extract_usage_from_message_end(payloads: list[dict[str, object]]) -> TokenUsage:
    for payload in reversed(payloads):
        if str(payload.get("type", "")).strip().lower() != "message_end":
            continue
        message_obj = payload.get("message")
        if not isinstance(message_obj, dict):
            continue
        message = cast("dict[str, object]", message_obj)
        if str(message.get("role", "")).strip().lower() != "assistant":
            continue
        usage_obj = message.get("usage")
        if not isinstance(usage_obj, dict):
            continue
        usage = cast("dict[str, object]", usage_obj)
        return TokenUsage(
            input_tokens=_coerce_optional_int(usage.get("input")),
            output_tokens=_coerce_optional_int(usage.get("output")),
            cache_read_input_tokens=_coerce_optional_int(usage.get("cacheRead")),
            cache_creation_input_tokens=_coerce_optional_int(usage.get("cacheWrite")),
            total_cost_usd=(
                float(total)
                if isinstance((cost := usage.get("cost")), dict)
                and (total := cast("dict[str, object]", cost).get("total")) is not None
                and isinstance(total, int | float)
                else None
            ),
        )
    return TokenUsage()


def _assistant_message_text(message: Mapping[str, object]) -> str | None:
    if str(message.get("role", "")).strip().lower() != "assistant":
        return None

    content_obj = message.get("content")
    if not isinstance(content_obj, list):
        return None

    texts: list[str] = []
    for part_obj in cast("list[object]", content_obj):
        if not isinstance(part_obj, dict):
            continue
        part = cast("dict[str, object]", part_obj)
        if str(part.get("type", "")).strip().lower() != "text":
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            texts.append(text)

    if not texts:
        return None
    return "\n".join(texts)


class PiHarnessExtractor(HarnessExtractor[ResolvedLaunchSpec]):
    """Extractor implementation for Pi artifacts and events."""

    def detect_session_id_from_event(self, event: HarnessEvent) -> str | None:
        if event.event_type != "session":
            return None
        session_id = event.payload.get("id")
        if isinstance(session_id, str) and session_id.strip():
            return session_id.strip()
        return None

    def detect_session_id_from_artifacts(
        self,
        *,
        spec: ResolvedLaunchSpec,
        launch_env: Mapping[str, str],
        child_cwd: Path,
        runtime_root: Path,
    ) -> str | None:
        _ = runtime_root
        if (
            spec.continue_session_id
            and spec.continue_session_id.strip()
            and not spec.continue_fork
        ):
            return spec.continue_session_id.strip()

        return _session_id_from_session_files(launch_env=launch_env, child_cwd=child_cwd)

    def extract_usage(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> TokenUsage:
        payloads = _iter_json_lines_artifact(artifacts, spawn_id, OUTPUT_FILENAME)
        return _extract_usage_from_message_end(payloads)

    def extract_session_id(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        candidates = _iter_session_id_candidates_from_artifacts(artifacts, spawn_id)
        if not candidates:
            return None
        return candidates[0]

    def extract_report(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        payloads = _iter_json_lines_artifact(artifacts, spawn_id, OUTPUT_FILENAME)
        for payload in reversed(payloads):
            if str(payload.get("type", "")).strip().lower() != "agent_end":
                continue
            messages_obj = payload.get("messages")
            if not isinstance(messages_obj, list):
                continue
            for message_obj in reversed(cast("list[object]", messages_obj)):
                if not isinstance(message_obj, dict):
                    continue
                text = _assistant_message_text(cast("dict[str, object]", message_obj))
                if text:
                    return text
        return None


PI_EXTRACTOR = PiHarnessExtractor()

__all__ = ["PI_EXTRACTOR", "PiHarnessExtractor", "_encode_cwd_for_session_dir"]
