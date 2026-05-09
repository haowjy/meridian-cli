# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
import math
from pathlib import Path

from meridian.lib.core.domain import TokenUsage
from meridian.lib.core.types import ArtifactKey, SpawnId
from meridian.lib.launch.constants import HISTORY_FILENAME, OUTPUT_FILENAME
from meridian.lib.launch.process.runner import _extract_primary_usage
from meridian.lib.state.artifact_store import ArtifactStore, InMemoryStore, make_artifact_key


class _Adapter:
    def __init__(self, usage: TokenUsage | None = None, *, should_fail: bool = False) -> None:
        self._usage = usage or TokenUsage()
        self._should_fail = should_fail

    def extract_usage(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> TokenUsage:
        _ = artifacts
        _ = spawn_id
        if self._should_fail:
            raise RuntimeError("usage extraction failed")
        return self._usage


class _InspectingAdapter:
    def __init__(self) -> None:
        self.seen_store: ArtifactStore | None = None
        self.seen_keys: set[ArtifactKey] = set()

    def extract_usage(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> TokenUsage:
        self.seen_store = artifacts
        for filename in (HISTORY_FILENAME, OUTPUT_FILENAME):
            key = make_artifact_key(spawn_id, filename)
            if artifacts.exists(key):
                self.seen_keys.add(key)
        return TokenUsage(input_tokens=1)


def _write_models_cache(project_root: Path) -> None:
    mars_dir = project_root / ".mars"
    mars_dir.mkdir(parents=True, exist_ok=True)
    (mars_dir / "models-cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "gpt-test-model",
                        "cost_input": 2.0,
                        "cost_output": 4.0,
                        "cost_cache_read": 0.5,
                        "cost_cache_write": 1.0,
                        "cost_reasoning": 3.0,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def test_extract_primary_usage_estimates_cost_and_returns_usage(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    _write_models_cache(project_root)
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    spawn_id = SpawnId("p1")
    adapter = _Adapter(
        TokenUsage(
            input_tokens=1000,
            output_tokens=2000,
            cache_read_input_tokens=500,
            cache_creation_input_tokens=250,
            reasoning_tokens=100,
        )
    )

    usage = _extract_primary_usage(
        harness_adapter=adapter,
        primary_spawn_id=spawn_id,
        project_root=project_root,
        model_id=" gpt-test-model ",
        log_dir=log_dir,
    )

    assert usage is not None
    assert usage.input_tokens == 1000
    assert usage.output_tokens == 2000
    assert usage.total_cost_usd is not None
    assert math.isclose(usage.total_cost_usd, 0.0108, rel_tol=0.0, abs_tol=1e-12)
    assert usage.cost_is_estimate is True


def test_extract_primary_usage_returns_none_when_missing_artifacts_and_usage_empty(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    spawn_id = SpawnId("p2")
    adapter = _Adapter(TokenUsage())

    usage = _extract_primary_usage(
        harness_adapter=adapter,
        primary_spawn_id=spawn_id,
        project_root=project_root,
        model_id=None,
        log_dir=log_dir,
    )

    assert usage is None


def test_extract_primary_usage_returns_none_when_extractor_raises(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    spawn_id = SpawnId("p3")
    adapter = _Adapter(should_fail=True)

    usage = _extract_primary_usage(
        harness_adapter=adapter,
        primary_spawn_id=spawn_id,
        project_root=project_root,
        model_id="gpt-test-model",
        log_dir=log_dir,
    )

    assert usage is None


def test_extract_primary_usage_uses_isolated_store_for_mirroring(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    history_bytes = b'{"event":"history"}\n'
    output_bytes = b'{"event":"output"}\n'
    (log_dir / HISTORY_FILENAME).write_bytes(history_bytes)
    (log_dir / OUTPUT_FILENAME).write_bytes(output_bytes)
    spawn_id = SpawnId("p4")
    adapter = _InspectingAdapter()

    usage = _extract_primary_usage(
        harness_adapter=adapter,
        primary_spawn_id=spawn_id,
        project_root=project_root,
        model_id=None,
        log_dir=log_dir,
    )

    assert usage is not None
    history_key = make_artifact_key(spawn_id, HISTORY_FILENAME)
    output_key = make_artifact_key(spawn_id, OUTPUT_FILENAME)
    assert isinstance(adapter.seen_store, InMemoryStore)
    assert history_key in adapter.seen_keys
    assert output_key in adapter.seen_keys
    assert adapter.seen_store.get(history_key) == history_bytes
    assert adapter.seen_store.get(output_key) == output_bytes
