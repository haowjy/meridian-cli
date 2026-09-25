from typing import Protocol

from meridian.lib.core.domain import TokenUsage
from meridian.lib.core.types import SpawnId
from meridian.lib.state.artifact_store import ArtifactStore


class SpawnExtractor(Protocol):
    def extract_usage(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> TokenUsage: ...
    def extract_session_id(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None: ...
    def extract_report(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None: ...
