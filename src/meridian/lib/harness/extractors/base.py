"""Typed harness extractor protocol."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Generic, Protocol, TypeVar, cast, runtime_checkable

from meridian.lib.core.domain import TokenUsage
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.adapter import ArtifactStore, SpawnExtractor
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

ExtractorSpecT = TypeVar("ExtractorSpecT", bound=ResolvedLaunchSpec, contravariant=True)


@runtime_checkable
class HarnessExtractor(SpawnExtractor, Protocol, Generic[ExtractorSpecT]):
    """Harness-owned extraction surface shared by subprocess and streaming."""

    def detect_session_id_from_event(self, event: RawHarnessEvent) -> str | None:
        """Best-effort extraction from one live event frame."""
        ...

    def detect_session_id_from_artifacts(
        self,
        *,
        spec: ExtractorSpecT,
        launch_env: Mapping[str, str],
        child_cwd: Path,
        runtime_root: Path,
    ) -> str | None:
        """Best-effort fallback extraction from harness-owned artifacts."""
        ...

    def extract_usage(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> TokenUsage:
        """Extract normalized usage from persisted artifacts."""
        ...

    def extract_session_id(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        """Extract session id directly from persisted run artifacts."""
        ...

    def extract_report(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        """Extract final report text from persisted artifacts."""
        ...


def session_from_mapping_with_keys(
    payload: Mapping[str, object],
    keys: tuple[str, ...],
) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for nested in payload.values():
        if isinstance(nested, dict):
            found = session_from_mapping_with_keys(cast("dict[str, object]", nested), keys)
            if found:
                return found
    return None


def normalize_harness_event_type(
    payload: Mapping[str, object],
    *,
    keys: tuple[str, ...] = ("event_type", "event", "type"),
) -> str:
    """Normalize event type fields to a stable dot-separated lowercase value."""

    raw_type: object = ""
    for key in keys:
        if key in payload:
            raw_type = payload.get(key, "")
            break
    return str(raw_type).strip().lower().replace("/", ".")


__all__ = [
    "HarnessExtractor",
    "normalize_harness_event_type",
    "session_from_mapping_with_keys",
]
