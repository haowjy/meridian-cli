"""ArtifactStore protocol and local/in-memory implementations."""

from collections.abc import MutableMapping
from pathlib import Path
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, PrivateAttr

from meridian.lib.core.types import ArtifactKey, SpawnId
from meridian.lib.state.atomic import atomic_write_bytes


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
    spawn_root_dir: Path | None = None

    @classmethod
    def for_runtime_root(cls, runtime_root: Path) -> "LocalStore":
        """Build a store whose history reads use the authoritative spawn tree."""

        return cls(
            root_dir=runtime_root / "artifacts",
            spawn_root_dir=runtime_root / "spawns",
        )

    def _history_read_path(self, rel: Path) -> Path | None:
        if self.spawn_root_dir is None or rel.name != "history.jsonl":
            return None
        candidate = self.spawn_root_dir / rel
        return candidate if candidate.is_file() else None

    def put(self, key: ArtifactKey, data: bytes) -> None:
        rel = _normalize_key(key)
        if self.spawn_root_dir is not None and rel.name == "history.jsonl":
            raise ValueError("Spawn history is authoritative in the spawn tree.")
        target = self.root_dir / rel
        atomic_write_bytes(target, data)

    def get(self, key: ArtifactKey) -> bytes:
        rel = _normalize_key(key)
        history_path = self._history_read_path(rel)
        if history_path is not None:
            return history_path.read_bytes()
        return (self.root_dir / rel).read_bytes()

    def exists(self, key: ArtifactKey) -> bool:
        rel = _normalize_key(key)
        if self._history_read_path(rel) is not None:
            return True
        return (self.root_dir / rel).exists()

    def delete(self, key: ArtifactKey) -> None:
        rel = _normalize_key(key)
        target = self.root_dir / rel
        if target.exists():
            target.unlink()

    def list_artifacts(self, spawn_id: str) -> list[ArtifactKey]:
        base = self.root_dir / spawn_id
        if not base.exists():
            return []

        artifacts: list[ArtifactKey] = []
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            artifacts.append(ArtifactKey(path.relative_to(self.root_dir).as_posix()))
        return artifacts


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
