"""Pi harness extractor."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

from meridian.lib.core.domain import TokenUsage
from meridian.lib.core.types import ArtifactKey, SpawnId
from meridian.lib.harness.adapter import ArtifactStore
from meridian.lib.harness.common import (
    OUTPUT_FILENAME,
    _coerce_optional_int,  # pyright: ignore[reportPrivateUsage]
    _iter_json_lines_artifact,  # pyright: ignore[reportPrivateUsage]
)
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.pi_failure import (
    compact_pi_failure_output,
    extract_pi_failure_from_history,
)
from meridian.lib.launch.constants import HISTORY_FILENAME
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

from .base import HarnessExtractor


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


def _read_artifact_text(artifacts: ArtifactStore, spawn_id: SpawnId, name: str) -> str:
    key = ArtifactKey(f"{spawn_id}/{name}")
    if not artifacts.exists(key):
        return ""
    return artifacts.get(key).decode("utf-8", errors="ignore")


class PiHarnessExtractor(HarnessExtractor[ResolvedLaunchSpec]):
    """Extractor implementation for Pi artifacts and events."""

    def detect_session_id_from_event(self, event: RawHarnessEvent) -> str | None:
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
        return spec.native_identity.session_id if spec.native_identity else None

    def extract_usage(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> TokenUsage:
        payloads = _iter_json_lines_artifact(artifacts, spawn_id, OUTPUT_FILENAME)
        return _extract_usage_from_message_end(payloads)

    def extract_session_id(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        candidates = _iter_session_id_candidates_from_artifacts(artifacts, spawn_id)
        if not candidates:
            return None
        return candidates[0]

    def extract_report(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        history_text = _read_artifact_text(artifacts, spawn_id, HISTORY_FILENAME)
        if history_text.strip():
            pi_failure = extract_pi_failure_from_history(history_text)
            if pi_failure:
                return pi_failure

        payloads = _iter_json_lines_artifact(artifacts, spawn_id, OUTPUT_FILENAME)
        for payload in reversed(payloads):
            event_type = str(payload.get("type", "")).strip().lower()
            if event_type == "response":
                command = str(payload.get("command", "")).strip().lower()
                if command == "prompt" and payload.get("success") is False:
                    error = payload.get("error")
                    if isinstance(error, str) and error.strip():
                        return compact_pi_failure_output(error.strip())
                    return "pi_prompt_rejected"
            if event_type != "agent_end":
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

__all__ = [
    "PI_EXTRACTOR",
    "PiHarnessExtractor",
]
