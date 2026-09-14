"""Write-ahead source invalidation for the disposable history projection.

Lock order: catchup, root mutation gate, database, source, marker gate.
Markers contain no history facts. A lost acknowledgement only causes replay.
"""

from __future__ import annotations

import hashlib
import re
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from meridian.lib.platform.locking import lock_file
from meridian.lib.state.atomic import atomic_write_text


class HistoryCoordinationError(ValueError):
    """Coordination cannot establish complete coverage; explicit rebuild is required."""


class HistorySource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["spawn", "sessions", "catalog"]
    key: str = ""

    @property
    def name(self) -> str:
        return hashlib.sha256(f"{self.kind}:{self.key}".encode()).hexdigest() + ".json"

    def lock_path(self, root: Path) -> Path:
        if self.kind == "spawn":
            if not self.key or self.key.startswith(".") or "/" in self.key or "\\" in self.key:
                raise HistoryCoordinationError("Unsafe spawn source key")
            return root / "locks" / "spawns" / f"{self.key}.lock"
        if self.key:
            raise HistoryCoordinationError("Unexpected log source key")
        name = (
            "sessions.jsonl.flock" if self.kind == "sessions" else "history-archives/catalog.lock"
        )
        return root / name


class DirtySource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source: HistorySource
    token: UUID


@dataclass(frozen=True)
class HistoryChanges:
    root: Path

    @property
    def mutation_lock(self) -> Path:
        return self.root / "locks" / "history-mutation.lock"

    @property
    def directory(self) -> Path:
        return self.root / "history-index" / "pending"

    @property
    def marker_lock(self) -> Path:
        # Stable and outside the replaceable database / pending namespace.
        return self.root / "locks" / "history-markers.lock"

    def mark(self, source: HistorySource, *, coalesce: bool = False) -> None:
        """Called BEFORE authority changes, under root and source locks."""
        source.lock_path(self.root)  # Validate before publishing an unresolvable intent.
        with lock_file(self.marker_lock):
            self._generation()
            path = self.directory / source.name
            if coalesce and path.exists():
                current = DirtySource.model_validate_json(path.read_bytes())
                if current.source != source:
                    raise HistoryCoordinationError("Mismatched pending history source")
                return
            marker = DirtySource(source=source, token=uuid4())
            atomic_write_text(self.directory / source.name, marker.model_dump_json() + "\n")

    def _generation(self) -> str:
        path = self.directory / "GENERATION"
        try:
            return str(UUID(path.read_text().strip()))
        except FileNotFoundError:
            generation = str(uuid4())
            atomic_write_text(path, generation + "\n")
            return generation
        except ValueError as exc:
            raise HistoryCoordinationError("Invalid history marker generation") from exc

    def capture(self) -> tuple[str, tuple[DirtySource, ...]]:
        """Capture a finite target without waiting for any source lock."""
        with lock_file(self.marker_lock):
            generation = self._generation()
            pending: list[DirtySource] = []
            for path in self.directory.glob("*.json"):
                if not re.fullmatch(r"[0-9a-f]{64}\.json", path.name):
                    raise HistoryCoordinationError(f"Unknown history marker: {path}")
                try:
                    marker = DirtySource.model_validate_json(path.read_bytes())
                    marker.source.lock_path(self.root)
                    if marker.source.name != path.name:
                        raise ValueError("Marker source does not match filename")
                except ValueError as exc:
                    raise HistoryCoordinationError(f"Invalid history marker: {path}") from exc
                pending.append(marker)
            return generation, tuple(pending)

    def acknowledge(self, marker: DirtySource) -> None:
        """After durable projection, remove only the token actually observed."""
        # A writer can reacquire its lock after projection. Do not let optional
        # cleanup extend a bounded query; retaining the marker safely replays it.
        with (
            suppress(TimeoutError),
            lock_file(marker.source.lock_path(self.root), timeout=0),
            lock_file(self.marker_lock, timeout=0),
        ):
            path = self.directory / marker.source.name
            try:
                current = DirtySource.model_validate_json(path.read_bytes())
            except FileNotFoundError:
                return
            if current == marker:
                # No fsync needed: resurrected markers safely replay committed rows.
                path.unlink()
