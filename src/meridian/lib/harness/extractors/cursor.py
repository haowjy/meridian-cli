"""Cursor harness extractor."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

from meridian.lib.core.domain import TokenUsage
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.adapter import ArtifactStore
from meridian.lib.harness.common import (
    OUTPUT_FILENAME,
    _coerce_optional_int,  # pyright: ignore[reportPrivateUsage]
    _extract_text,  # pyright: ignore[reportPrivateUsage]
    _iter_json_lines_artifact,  # pyright: ignore[reportPrivateUsage]
    extract_session_id_from_artifacts_with_patterns,
    extract_usage_from_artifacts,
    iter_nested_dicts,
)
from meridian.lib.harness.connections.base import HarnessEvent
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

from .base import HarnessExtractor, normalize_harness_event_type, session_from_mapping_with_keys

_SESSION_ID_KEYS: tuple[str, ...] = (
    "session_id",
    "sessionId",
    "sessionID",
    "session",
    "conversation_id",
    "conversationId",
)


def _is_result_event(payload: Mapping[str, object]) -> bool:
    event_type = normalize_harness_event_type(payload)
    return event_type == "result" or event_type.endswith(".result")


def _find_session_id(payload: Mapping[str, object]) -> str | None:
    return session_from_mapping_with_keys(payload, _SESSION_ID_KEYS)


class CursorHarnessExtractor(HarnessExtractor[ResolvedLaunchSpec]):
    """Extractor implementation for Cursor artifacts and events."""

    def detect_session_id_from_event(self, event: HarnessEvent) -> str | None:
        return _find_session_id(event.payload)

    def detect_session_id_from_artifacts(
        self,
        *,
        spec: ResolvedLaunchSpec,
        launch_env: Mapping[str, str],
        child_cwd: Path,
        runtime_root: Path,
    ) -> str | None:
        _ = launch_env, child_cwd, runtime_root
        if spec.continue_session_id and spec.continue_session_id.strip():
            return spec.continue_session_id.strip()
        return None

    def extract_usage(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> TokenUsage:
        last_usage: dict[str, object] | None = None
        for payload in _iter_json_lines_artifact(artifacts, spawn_id, OUTPUT_FILENAME):
            if not _is_result_event(payload):
                continue

            for nested in iter_nested_dicts(payload):
                usage_obj = nested.get("usage")
                if isinstance(usage_obj, dict):
                    last_usage = cast("dict[str, object]", usage_obj)

        if last_usage is None:
            return extract_usage_from_artifacts(artifacts, spawn_id)

        return TokenUsage(
            input_tokens=_coerce_optional_int(last_usage.get("inputTokens")),
            output_tokens=_coerce_optional_int(last_usage.get("outputTokens")),
            cache_read_input_tokens=_coerce_optional_int(last_usage.get("cacheReadTokens")),
            cache_creation_input_tokens=_coerce_optional_int(last_usage.get("cacheWriteTokens")),
        )

    def extract_session_id(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        return extract_session_id_from_artifacts_with_patterns(
            artifacts,
            spawn_id,
            json_keys=_SESSION_ID_KEYS,
        )

    def extract_report(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        final_report: str | None = None
        last_assistant_text: str | None = None
        for payload in _iter_json_lines_artifact(artifacts, spawn_id, OUTPUT_FILENAME):
            if _is_result_event(payload):
                for nested in iter_nested_dicts(payload):
                    for key in ("result", "text", "output", "content", "message"):
                        if key not in nested:
                            continue
                        text = _extract_text(nested.get(key))
                        if text:
                            final_report = text
                continue

            if normalize_harness_event_type(payload) != "assistant":
                continue

            message = payload.get("message")
            if not isinstance(message, dict):
                continue
            text = _extract_text(cast("dict[str, object]", message))
            if text:
                last_assistant_text = text

        return final_report or last_assistant_text


CURSOR_EXTRACTOR = CursorHarnessExtractor()

__all__ = ["CURSOR_EXTRACTOR", "CursorHarnessExtractor"]
