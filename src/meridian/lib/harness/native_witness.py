"""Native source freshness observations, independent of disposable projections."""

from __future__ import annotations

import json
from dataclasses import astuple, dataclass
from pathlib import Path
from typing import Protocol


class Witness(Protocol):
    """A native source's freshness: equal encodings mean the source has not changed.

    A file's is ``FileWitness``; a harness whose sessions share one store gives its own.
    """

    def encode(self) -> str: ...

    @property
    def activity_ns(self) -> int: ...


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


def file_witness(path: Path) -> FileWitness:
    stat = path.stat()
    return FileWitness(stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
