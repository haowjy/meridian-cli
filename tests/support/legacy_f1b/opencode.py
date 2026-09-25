"""OpenCode harness extractor."""

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
    coerce_optional_float,
    extract_usage_from_artifacts,
)
from tests.support.legacy_f1b.opencode_report import (
    extract_opencode_report,
    extract_opencode_session_id,
    extract_opencode_session_id_from_artifacts,
)

from .base import HarnessExtractor


class OpenCodeHarnessExtractor(HarnessExtractor[ResolvedLaunchSpec]):
    """Extractor implementation for OpenCode artifacts and events."""

    def detect_session_id_from_event(self, event: RawHarnessEvent) -> str | None:
        return extract_opencode_session_id(dict(event.payload))

    def detect_session_id_from_artifacts(
        self,
        *,
        spec: ResolvedLaunchSpec,
        launch_env: Mapping[str, str],
        child_cwd: Path,
        runtime_root: Path,
    ) -> str | None:
        if spec.continue_session_id and spec.continue_session_id.strip():
            return spec.continue_session_id.strip()
        _ = launch_env, child_cwd, runtime_root
        return None

    def extract_usage(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> TokenUsage:
        specific = _extract_opencode_usage(artifacts, spawn_id)
        if specific != TokenUsage():
            return specific
        return extract_usage_from_artifacts(artifacts, spawn_id)

    def extract_session_id(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        return extract_opencode_session_id_from_artifacts(artifacts, spawn_id)

    def extract_report(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        return extract_opencode_report(artifacts, spawn_id)


OPENCODE_EXTRACTOR = OpenCodeHarnessExtractor()

__all__ = ["OPENCODE_EXTRACTOR", "OpenCodeHarnessExtractor"]


def _try_parse_opencode_usage(payload: dict[str, object]) -> TokenUsage | None:
    properties_obj = payload.get("properties")
    properties = (
        cast("dict[str, object]", properties_obj) if isinstance(properties_obj, dict) else None
    )
    nested_info_obj = properties.get("info") if properties is not None else None
    nested_info = (
        cast("dict[str, object]", nested_info_obj) if isinstance(nested_info_obj, dict) else None
    )
    nested_tokens_obj = nested_info.get("tokens") if nested_info is not None else None
    nested_tokens = (
        cast("dict[str, object]", nested_tokens_obj)
        if isinstance(nested_tokens_obj, dict)
        else None
    )
    if nested_tokens is not None:
        nested_cache_obj = nested_tokens.get("cache")
        nested_cache = (
            cast("dict[str, object]", nested_cache_obj)
            if isinstance(nested_cache_obj, dict)
            else {}
        )
        nested_usage = TokenUsage(
            input_tokens=_coerce_optional_int(nested_tokens.get("input")),
            output_tokens=_coerce_optional_int(nested_tokens.get("output")),
            cache_read_input_tokens=_coerce_optional_int(nested_cache.get("read")),
            cache_creation_input_tokens=_coerce_optional_int(nested_cache.get("write")),
            reasoning_tokens=_coerce_optional_int(nested_tokens.get("reasoning")),
            total_cost_usd=coerce_optional_float(
                nested_info.get("cost") if nested_info is not None else None
            ),
        )
        if any(
            value is not None
            for value in (
                nested_usage.input_tokens,
                nested_usage.output_tokens,
                nested_usage.cache_read_input_tokens,
                nested_usage.cache_creation_input_tokens,
                nested_usage.reasoning_tokens,
            )
        ):
            return nested_usage

    info_obj = payload.get("info")
    info = cast("dict[str, object]", info_obj) if isinstance(info_obj, dict) else payload
    usage_obj = payload.get("usage")
    usage_source = cast("dict[str, object]", usage_obj) if isinstance(usage_obj, dict) else info
    usage = usage_source
    cost_obj = payload.get("cost")
    cost_source = cast("dict[str, object]", cost_obj) if isinstance(cost_obj, dict) else payload
    legacy_usage = TokenUsage(
        input_tokens=_coerce_optional_int(usage.get("input_tokens") or usage.get("input")),
        output_tokens=_coerce_optional_int(usage.get("output_tokens") or usage.get("output")),
        cache_read_input_tokens=_coerce_optional_int(
            usage.get("cache_read_input_tokens") or usage.get("cache_read")
        ),
        cache_creation_input_tokens=_coerce_optional_int(
            usage.get("cache_creation_input_tokens") or usage.get("cache_write")
        ),
        reasoning_tokens=_coerce_optional_int(
            usage.get("reasoning_tokens") or usage.get("reasoning")
        ),
        total_cost_usd=coerce_optional_float(cost_source.get("total_cost_usd")),
    )
    if any(
        value is not None
        for value in (
            legacy_usage.input_tokens,
            legacy_usage.output_tokens,
            legacy_usage.cache_read_input_tokens,
            legacy_usage.cache_creation_input_tokens,
            legacy_usage.reasoning_tokens,
        )
    ):
        return legacy_usage
    return None


def _extract_opencode_usage(artifacts: ArtifactStore, spawn_id: SpawnId) -> TokenUsage:
    last: TokenUsage | None = None
    for payload in _iter_json_lines_artifact(artifacts, spawn_id, OUTPUT_FILENAME):
        event_type = (
            str(payload.get("event", payload.get("type", payload.get("event_type", ""))))
            .strip()
            .lower()
        )
        if event_type not in {"session.idle", "message.updated"}:
            continue
        usage = _try_parse_opencode_usage(payload)
        if usage is not None:
            last = usage
    return last or TokenUsage()
