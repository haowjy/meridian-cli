"""Native source freshness observations, independent of disposable projections."""

from __future__ import annotations

import json
from dataclasses import astuple, dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileWitness:
    device: int
    inode: int
    size: int
    mtime_ns: int

    def encode(self) -> str:
        return json.dumps(("file", astuple(self)), separators=(",", ":"))

    @property
    def activity_ns(self) -> int:
        return self.mtime_ns


@dataclass(frozen=True)
class OpenCodeV1Witness:
    part_count: int
    part_updated_ms: int | None
    message_count: int
    message_updated_ms: int | None
    session_updated_ms: int

    def encode(self) -> str:
        return json.dumps(("opencode-v1", astuple(self)), separators=(",", ":"))

    @property
    def activity_ns(self) -> int:
        return (
            max(self.session_updated_ms, self.message_updated_ms or 0, self.part_updated_ms or 0)
            * 1_000_000
        )


@dataclass(frozen=True)
class OpenCodeV2Witness:
    message_count: int
    max_seq: int | None
    message_updated_ms: int | None
    session_updated_ms: int

    def encode(self) -> str:
        return json.dumps(("opencode-v2", astuple(self)), separators=(",", ":"))

    @property
    def activity_ns(self) -> int:
        return max(self.session_updated_ms, self.message_updated_ms or 0) * 1_000_000


Witness = FileWitness | OpenCodeV1Witness | OpenCodeV2Witness


def file_witness(path: Path) -> FileWitness:
    stat = path.stat()
    return FileWitness(stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
