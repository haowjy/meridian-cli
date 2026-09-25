"""Claude harness extractor."""

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
    _iter_json_lines_artifact,  # pyright: ignore[reportPrivateUsage]
    coerce_optional_float,
    extract_claude_report,
    extract_usage_from_artifacts,
    read_session_id_artifact,
)
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

from .base import HarnessExtractor, normalize_harness_event_type


class ClaudeHarnessExtractor(HarnessExtractor[ResolvedLaunchSpec]):
    """Extractor implementation for Claude artifacts and events."""

    def detect_session_id_from_event(self, event: RawHarnessEvent) -> str | None:
        if event.event_type not in {"system", "result", "assistant", "user"}:
            return None
        for key in ("session_id", "sessionId", "sessionID"):
            value = event.payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def detect_session_id_from_artifacts(
        self,
        *,
        spec: ResolvedLaunchSpec,
        launch_env: Mapping[str, str],
        child_cwd: Path,
        runtime_root: Path,
    ) -> str | None:
        _ = runtime_root, launch_env, child_cwd
        if spec.continue_session_id and spec.continue_session_id.strip():
            return spec.continue_session_id.strip()
        seeded_session_id = spec.claude_session_seed_id
        if seeded_session_id:
            return seeded_session_id
        return None

    def extract_usage(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> TokenUsage:
        specific = _extract_claude_usage(artifacts, spawn_id)
        if specific != TokenUsage():
            return specific
        return extract_usage_from_artifacts(artifacts, spawn_id)

    def extract_session_id(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        for payload in _iter_json_lines_artifact(artifacts, spawn_id, OUTPUT_FILENAME):
            session_id = self.detect_session_id_from_event(RawHarnessEvent(
                event_type=normalize_harness_event_type(payload),
                harness_id="claude", payload=payload,
            ))
            if session_id:
                return session_id
        return read_session_id_artifact(artifacts, spawn_id)

    def extract_report(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        return extract_claude_report(artifacts, spawn_id)


CLAUDE_EXTRACTOR = ClaudeHarnessExtractor()

__all__ = ["CLAUDE_EXTRACTOR", "ClaudeHarnessExtractor"]


def _extract_claude_usage(artifacts: ArtifactStore, spawn_id: SpawnId) -> TokenUsage:
    input_tokens = output_tokens = cache_read = cache_creation = 0
    cost: float | None = None
    found = False
    for payload in _iter_json_lines_artifact(artifacts, spawn_id, OUTPUT_FILENAME):
        if str(payload.get("type", payload.get("event", ""))).strip().lower() != "result":
            continue
        found = True
        # Prefer payload-level exact total cost when available
        payload_cost = coerce_optional_float(payload.get("total_cost_usd"))
        if payload_cost is not None:
            cost = payload_cost
        # Real Claude events use camelCase modelUsage; test fixtures use snake_case usage
        model_usage = payload.get("modelUsage")
        if isinstance(model_usage, dict):
            model_usage_map = cast("dict[str, object]", model_usage)
            for item_obj in model_usage_map.values():
                if not isinstance(item_obj, dict):
                    continue
                item = cast("dict[str, object]", item_obj)
                input_tokens += _coerce_optional_int(item.get("inputTokens")) or 0
                output_tokens += _coerce_optional_int(item.get("outputTokens")) or 0
                cache_read += _coerce_optional_int(item.get("cacheReadInputTokens")) or 0
                cache_creation += _coerce_optional_int(item.get("cacheCreationInputTokens")) or 0
                # Only sum per-model costs if no payload-level total was provided
                if cost is None:
                    item_cost = coerce_optional_float(item.get("costUSD"))
                    if item_cost is not None:
                        cost = (cost or 0.0) + item_cost
        # Also support test fixture format with snake_case "usage" field
        usage = payload.get("usage")
        if isinstance(usage, dict) and not isinstance(model_usage, dict):
            usage_map = cast("dict[str, object]", usage)
            for item_obj in usage_map.values():
                if not isinstance(item_obj, dict):
                    continue
                item = cast("dict[str, object]", item_obj)
                input_tokens += _coerce_optional_int(item.get("input_tokens")) or 0
                output_tokens += _coerce_optional_int(item.get("output_tokens")) or 0
                cache_read += _coerce_optional_int(item.get("cache_read_input_tokens")) or 0
                cache_creation += _coerce_optional_int(item.get("cache_creation_input_tokens")) or 0
            if cost is None:
                payload_cost = coerce_optional_float(payload.get("total_cost_usd"))
                if payload_cost is not None:
                    cost = payload_cost
    if not found:
        return TokenUsage()
    return TokenUsage(
        input_tokens=input_tokens or None,
        output_tokens=output_tokens or None,
        cache_read_input_tokens=cache_read or None,
        cache_creation_input_tokens=cache_creation or None,
        total_cost_usd=cost,
    )
