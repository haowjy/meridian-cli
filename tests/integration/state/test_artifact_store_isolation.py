"""Artifact storage is isolated from spawn-owned files."""

from pathlib import Path

from meridian.lib.core.types import SpawnId
from meridian.lib.launch.artifact_io import read_artifact_text
from meridian.lib.state.artifact_store import LocalStore, make_artifact_key


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_artifact_bytes_remain_readable(tmp_path: Path) -> None:
    spawn_id = SpawnId("p1")
    legacy_path = tmp_path / "artifacts" / str(spawn_id) / "events.jsonl"
    _write(legacy_path, "legacy history\n")
    artifacts = LocalStore(root_dir=tmp_path / "artifacts")
    key = make_artifact_key(spawn_id, "events.jsonl")

    assert artifacts.exists(key)
    assert artifacts.get(key) == b"legacy history\n"
    assert read_artifact_text(artifacts, spawn_id, "events.jsonl") == "legacy history\n"
    assert key in artifacts.list_artifacts(str(spawn_id))


def test_artifact_reads_never_redirect_to_spawn_files(tmp_path: Path) -> None:
    spawn_id = SpawnId("p1")
    canonical_path = tmp_path / "spawns" / str(spawn_id) / "events.jsonl"
    legacy_path = tmp_path / "artifacts" / str(spawn_id) / "events.jsonl"
    _write(canonical_path, "canonical history\n")
    _write(legacy_path, "conflicting legacy history\n")
    artifacts = LocalStore(root_dir=tmp_path / "artifacts")
    key = make_artifact_key(spawn_id, "events.jsonl")

    assert artifacts.exists(key)
    assert artifacts.get(key) == b"conflicting legacy history\n"
    assert read_artifact_text(artifacts, spawn_id, "events.jsonl") == "conflicting legacy history\n"
    assert artifacts.list_artifacts(str(spawn_id)).count(key) == 1
