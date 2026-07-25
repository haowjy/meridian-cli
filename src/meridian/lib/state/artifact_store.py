"""ArtifactStore protocol and local/in-memory implementations."""

from collections.abc import MutableMapping
from pathlib import Path
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, PrivateAttr

from meridian.lib.core.types import ArtifactKey, SpawnId
from meridian.lib.launch.constants import HISTORY_FILENAME
from meridian.lib.state.atomic import atomic_write_bytes
from meridian.lib.state.paths import resolve_spawn_history_path


class ArtifactStore(Protocol):
    """Read/write interface for run artifacts."""

    def put(self, key: ArtifactKey, data: bytes) -> None: ...

    def get(self, key: ArtifactKey) -> bytes: ...

    def exists(self, key: ArtifactKey) -> bool: ...

    def delete(self, key: ArtifactKey) -> None: ...

    def list_artifacts(self, spawn_id: str) -> list[ArtifactKey]: ...


def make_artifact_key(spawn_id: SpawnId | str, name: str) -> ArtifactKey:
    """Build an artifact key from run ID and artifact name/path."""

    return ArtifactKey(f"{spawn_id}/{name}")


def _normalize_key(key: ArtifactKey) -> Path:
    rel = Path(str(key))
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"Artifact key must be a safe relative path: {key}")
    return rel


def _empty_bytes_map() -> MutableMapping[ArtifactKey, bytes]:
    return cast("MutableMapping[ArtifactKey, bytes]", {})


class LocalStore(BaseModel):
    """Filesystem-backed artifact store rooted at one directory."""

    model_config = ConfigDict()

    root_dir: Path

    def _read_path(self, rel: Path) -> Path:
        if len(rel.parts) >= 2 and rel.name == HISTORY_FILENAME:
            resolved = resolve_spawn_history_path(
                self.root_dir.parent,
                rel.parts[0],
                relative_path=Path(*rel.parts[1:]),
            )
            if resolved is not None:
                return resolved
        return self.root_dir / rel

    def put(self, key: ArtifactKey, data: bytes) -> None:
        rel = _normalize_key(key)
        target = self.root_dir / rel
        atomic_write_bytes(target, data)

    def get(self, key: ArtifactKey) -> bytes:
        rel = _normalize_key(key)
        return self._read_path(rel).read_bytes()

    def exists(self, key: ArtifactKey) -> bool:
        rel = _normalize_key(key)
        return self._read_path(rel).exists()

    def delete(self, key: ArtifactKey) -> None:
        rel = _normalize_key(key)
        target = self.root_dir / rel
        if target.exists():
            target.unlink()

    def list_artifacts(self, spawn_id: str) -> list[ArtifactKey]:
        base = self.root_dir / spawn_id
        artifacts: set[ArtifactKey] = set()
        if base.is_dir():
            for path in sorted(base.rglob("*")):
                if not path.is_file():
                    continue
                artifacts.add(ArtifactKey(path.relative_to(self.root_dir).as_posix()))

        canonical_base = self.root_dir.parent / "spawns" / spawn_id
        if canonical_base.is_dir():
            for path in canonical_base.rglob(HISTORY_FILENAME):
                if path.is_file():
                    relative = path.relative_to(canonical_base)
                    artifacts.add(ArtifactKey(f"{spawn_id}/{relative.as_posix()}"))
        return sorted(artifacts, key=str)


class InMemoryStore(BaseModel):
    """Process-local in-memory artifact store."""

    model_config = ConfigDict()

    _data: MutableMapping[ArtifactKey, bytes] = PrivateAttr(default_factory=_empty_bytes_map)

    def put(self, key: ArtifactKey, data: bytes) -> None:
        _normalize_key(key)
        self._data[key] = data

    def get(self, key: ArtifactKey) -> bytes:
        _normalize_key(key)
        return self._data[key]

    def exists(self, key: ArtifactKey) -> bool:
        _normalize_key(key)
        return key in self._data

    def delete(self, key: ArtifactKey) -> None:
        _normalize_key(key)
        self._data.pop(key, None)

    def list_artifacts(self, spawn_id: str) -> list[ArtifactKey]:
        prefix = f"{spawn_id}/"
        matches = [key for key in self._data if str(key).startswith(prefix)]
        return sorted(matches, key=str)
