"""Codex harness extractor."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from meridian.lib.core.domain import TokenUsage
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.adapter import ArtifactStore
from meridian.lib.harness.codex_rollout import (
    CODEX_ROLLOUT_FILENAME_RE,
    resolve_codex_home,
    resolve_rollout_session_id,
)
from meridian.lib.harness.common import (
    OUTPUT_FILENAME,
    _coerce_optional_int,  # pyright: ignore[reportPrivateUsage]
    _iter_json_lines_artifact,  # pyright: ignore[reportPrivateUsage]
    extract_codex_report,
    extract_session_id_from_artifacts_with_patterns,
    extract_usage_from_artifacts,
)
from meridian.lib.harness.connections.base import HarnessEvent
from meridian.lib.harness.cost import estimate_usage_cost
from meridian.lib.harness.launch_spec import CodexLaunchSpec

from .base import HarnessExtractor, session_from_mapping_with_keys

_SESSION_ID_TEXT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bcodex\s+resume\s+([A-Za-z0-9][A-Za-z0-9._:-]{5,})\b", re.IGNORECASE),
    re.compile(r"\bresume\s+([A-Za-z0-9][A-Za-z0-9._:-]{5,})\b", re.IGNORECASE),
)


def _resolve_rollout_session_id(path: Path, project_root: Path) -> str | None:
    return resolve_rollout_session_id(path, project_root)


def _detect_primary_session_id(
    *,
    child_cwd: Path,
    launch_env: Mapping[str, str],
) -> str | None:
    sessions_root = resolve_codex_home(launch_env) / "sessions"

    if not sessions_root.is_dir():
        return None

    project_root = child_cwd.resolve()
    candidates: list[tuple[float, Path]] = []
    for candidate in sessions_root.rglob("rollout-*.jsonl"):
        if CODEX_ROLLOUT_FILENAME_RE.match(candidate.name) is None:
            continue
        try:
            modified_at = candidate.stat().st_mtime
        except OSError:
            continue
        candidates.append((modified_at, candidate))

    for _, candidate in sorted(candidates, key=lambda item: item[0], reverse=True):
        resolved = _resolve_rollout_session_id(candidate, project_root)
        if resolved:
            return resolved
    return None


class CodexHarnessExtractor(HarnessExtractor[CodexLaunchSpec]):
    """Extractor implementation for Codex artifacts and events."""

    def detect_session_id_from_event(self, event: HarnessEvent) -> str | None:
        return session_from_mapping_with_keys(
            event.payload,
            (
                "threadId",
                "thread_id",
                "session_id",
                "sessionId",
                "sessionID",
                "conversation_id",
                "conversationId",
            ),
        )

    def detect_session_id_from_artifacts(
        self,
        *,
        spec: CodexLaunchSpec,
        launch_env: Mapping[str, str],
        child_cwd: Path,
        runtime_root: Path,
    ) -> str | None:
        _ = runtime_root
        if spec.continue_session_id and spec.continue_session_id.strip():
            return spec.continue_session_id.strip()
        return _detect_primary_session_id(child_cwd=child_cwd, launch_env=launch_env)

    def extract_usage(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> TokenUsage:
        specific = _extract_codex_usage(artifacts, spawn_id)
        if specific != TokenUsage():
            return specific
        return extract_usage_from_artifacts(artifacts, spawn_id)

    def extract_session_id(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        return extract_session_id_from_artifacts_with_patterns(
            artifacts,
            spawn_id,
            json_keys=(
                "session_id",
                "sessionId",
                "sessionID",
                "conversation_id",
                "conversationId",
                "thread_id",
                "threadId",
            ),
            text_patterns=_SESSION_ID_TEXT_PATTERNS,
        )

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
    model_id: str | None = None
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
                    total = cast("dict[str, object]", total_obj)
                    last_total_usage = total
                # Try to extract model from the event payload
                raw_model = payload.get("model") or _nested_get(payload, "payload", "model")
                if isinstance(raw_model, str) and raw_model.strip():
                    model_id = raw_model.strip()
        # Test fixture / fallback: turn/completed with snake_case usage
        elif event_type == "turn.completed":
            usage_obj = payload.get("usage")
            if not isinstance(usage_obj, dict):
                usage_obj = _nested_get(payload, "payload", "usage")
            if isinstance(usage_obj, dict):
                usage = cast("dict[str, object]", usage_obj)
                last_total_usage = usage
                raw_model = payload.get("model") or _nested_get(payload, "payload", "model")
                if isinstance(raw_model, str) and raw_model.strip():
                    model_id = raw_model.strip()
    if last_total_usage is None:
        return TokenUsage()
    usage = TokenUsage(
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
    return estimate_usage_cost(model_id=model_id, usage=usage)
