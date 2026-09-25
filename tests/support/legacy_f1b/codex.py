"""Codex harness extractor."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

from meridian.lib.core.domain import TokenUsage
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.state.artifact_store import ArtifactStore
from tests.support.legacy_f1b.common import (
    OUTPUT_FILENAME,
    _coerce_optional_int,  # pyright: ignore[reportPrivateUsage]
    _iter_json_lines_artifact,  # pyright: ignore[reportPrivateUsage]
    extract_codex_report,
    extract_usage_from_artifacts,
)

from .base import HarnessExtractor, normalize_harness_event_type


def _owned_session_id(payload: Mapping[str, object], event_type: str) -> str | None:
    if event_type.replace("/", ".") not in {"thread.started", "session_id"}:
        return None
    for key in ("thread_id", "threadId", "session_id"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    thread = payload.get("thread")
    if isinstance(thread, dict):
        value = thread.get("id")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


class CodexHarnessExtractor(HarnessExtractor[ResolvedLaunchSpec]):
    """Extractor implementation for Codex artifacts and events."""

    def detect_session_id_from_event(self, event: RawHarnessEvent) -> str | None:
        return _owned_session_id(event.payload, event.event_type)

    def detect_session_id_from_artifacts(
        self,
        *,
        spec: ResolvedLaunchSpec,
        launch_env: Mapping[str, str],
        child_cwd: Path,
        runtime_root: Path,
    ) -> str | None:
        _ = runtime_root
        if spec.continue_session_id and spec.continue_session_id.strip():
            return spec.continue_session_id.strip()
        _ = child_cwd, launch_env
        return None

    def extract_usage(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> TokenUsage:
        specific = _extract_codex_usage(artifacts, spawn_id)
        if specific != TokenUsage():
            return specific
        return extract_usage_from_artifacts(artifacts, spawn_id)

    def extract_session_id(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        for payload in _iter_json_lines_artifact(artifacts, spawn_id, OUTPUT_FILENAME):
            session_id = _owned_session_id(payload, normalize_harness_event_type(payload))
            if session_id:
                return session_id
        return None

    def extract_report(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        return extract_codex_report(artifacts, spawn_id)


CODEX_EXTRACTOR = CodexHarnessExtractor()

__all__ = ["CODEX_EXTRACTOR", "CodexHarnessExtractor"]


def _nested_get(payload: dict[str, object], *keys: str) -> object:
    current: object = payload
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = cast("Mapping[str, object]", current).get(key)
    return current


def _extract_codex_usage(artifacts: ArtifactStore, spawn_id: SpawnId) -> TokenUsage:
    last_total_usage: dict[str, object] | None = None
    for payload in _iter_json_lines_artifact(artifacts, spawn_id, OUTPUT_FILENAME):
        event_type = (
            str(payload.get("event_type", payload.get("type", payload.get("event", ""))))
            .strip()
            .lower()
            .replace("/", ".")
        )
        # Real Codex events: thread/tokenUsage/updated with tokenUsage.total (camelCase)
        if event_type == "thread.tokenusage.updated":
            token_usage_obj = payload.get("tokenUsage") or _nested_get(
                payload, "payload", "tokenUsage"
            )
            if isinstance(token_usage_obj, dict):
                token_usage = cast("dict[str, object]", token_usage_obj)
                total_obj = token_usage.get("total")
                if isinstance(total_obj, dict):
                    last_total_usage = cast("dict[str, object]", total_obj)
        # Test fixture / fallback: turn/completed with snake_case usage
        elif event_type == "turn.completed":
            usage_obj = payload.get("usage")
            if not isinstance(usage_obj, dict):
                usage_obj = _nested_get(payload, "payload", "usage")
            if isinstance(usage_obj, dict):
                last_total_usage = cast("dict[str, object]", usage_obj)
    if last_total_usage is None:
        return TokenUsage()
    return TokenUsage(
        input_tokens=_coerce_optional_int(
            last_total_usage.get("inputTokens") or last_total_usage.get("input_tokens")
        ),
        output_tokens=_coerce_optional_int(
            last_total_usage.get("outputTokens") or last_total_usage.get("output_tokens")
        ),
        cache_read_input_tokens=_coerce_optional_int(
            last_total_usage.get("cachedInputTokens") or last_total_usage.get("cached_input_tokens")
        ),
        cache_creation_input_tokens=_coerce_optional_int(
            last_total_usage.get("cacheCreationInputTokens")
            or last_total_usage.get("cache_creation_input_tokens")
        ),
        reasoning_tokens=_coerce_optional_int(
            last_total_usage.get("reasoningOutputTokens")
            or last_total_usage.get("reasoning_output_tokens")
        ),
    )
