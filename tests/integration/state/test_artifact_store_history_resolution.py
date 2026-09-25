"""Canonical and legacy spawn-history resolution behavior."""

from pathlib import Path

from meridian.lib.core.types import SpawnId
from meridian.lib.launch.artifact_io import read_artifact_text
from meridian.lib.launch.constants import HISTORY_FILENAME
from meridian.lib.state.artifact_store import LocalStore, make_artifact_key
from meridian.lib.state.paths import resolve_spawn_history_path, resolve_spawn_output_path


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_legacy_artifact_history_remains_readable(tmp_path: Path) -> None:
    spawn_id = SpawnId("p1")
    legacy_path = tmp_path / "artifacts" / str(spawn_id) / HISTORY_FILENAME
    _write(legacy_path, "legacy history\n")
    artifacts = LocalStore(root_dir=tmp_path / "artifacts")
    key = make_artifact_key(spawn_id, HISTORY_FILENAME)

    assert resolve_spawn_history_path(tmp_path, spawn_id) == legacy_path
    assert resolve_spawn_output_path(tmp_path, str(spawn_id)) is None
    assert artifacts.exists(key)
    assert artifacts.get(key) == b"legacy history\n"
    assert read_artifact_text(artifacts, spawn_id, HISTORY_FILENAME) == "legacy history\n"
    assert key in artifacts.list_artifacts(str(spawn_id))


def test_artifact_reads_never_redirect_to_spawn_history(tmp_path: Path) -> None:
    spawn_id = SpawnId("p1")
    canonical_path = tmp_path / "spawns" / str(spawn_id) / HISTORY_FILENAME
    legacy_path = tmp_path / "artifacts" / str(spawn_id) / HISTORY_FILENAME
    _write(canonical_path, "canonical history\n")
    _write(legacy_path, "conflicting legacy history\n")
    artifacts = LocalStore(root_dir=tmp_path / "artifacts")
    key = make_artifact_key(spawn_id, HISTORY_FILENAME)

    assert resolve_spawn_history_path(tmp_path, spawn_id) == canonical_path
    assert resolve_spawn_output_path(tmp_path, str(spawn_id)) is None
    assert artifacts.exists(key)
    assert artifacts.get(key) == b"conflicting legacy history\n"
    assert (
        read_artifact_text(artifacts, spawn_id, HISTORY_FILENAME) == "conflicting legacy history\n"
    )
    assert artifacts.list_artifacts(str(spawn_id)).count(key) == 1
