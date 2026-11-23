"""What ``agy``'s model context holds, read from the conversation's transcript.

``agy`` has no compaction hook (its plugin events in 1.5.15 are ``SessionStart``,
``PreInvocation``, ``PostInvocation``, ``PreToolUse``, ``PostToolUse`` and ``Stop``), and a
compaction keeps neither of Meridian's injected blocks. The model's context is the
transcript's steps after its last ``CHECKPOINT`` (``agy`` 1.5.14 appends; nothing earlier is
rewritten), so a block is held when an injected message after that step carries it.

``guard.sh`` greps the bytes after a recorded offset for ``CHECKPOINT``, and ``transcript.py``
documents the format for the session log reader. A format change for a newer ``agy`` updates
all three.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import NamedTuple, cast

CHECKPOINT = b'"type":"CHECKPOINT"'
_SYSTEM_MESSAGE = b'"type":"SYSTEM_MESSAGE"'
# What ``InboxEntry.render`` opens a queued message with. Such a message quotes whatever its
# sender wrote, including a block's heading, so it never counts as the block itself.
_QUEUED = re.compile(r"\[meridian:[^\]\s]+\]")


class Segment(NamedTuple):
    """The injected messages in the model's current context, and where this read ended.

    ``end`` is the offset after the last complete line: the transcript is written
    mid-turn, so an unfinished last line is left for the next read.
    """

    texts: list[str]
    end: int

    def holds(self, text: str) -> bool:
        return any(text in held for held in self.texts)


def read_segment(path: str | None) -> Segment:
    data = _read(path, 0)
    end = data.rfind(b"\n") + 1
    texts: list[str] = []
    for line in data[:end].split(b"\n"):
        if CHECKPOINT in line:
            texts = []
        elif _SYSTEM_MESSAGE in line:
            try:
                step = json.loads(line)
            except ValueError:
                continue
            content = (
                cast("dict[str, object]", step).get("content") if isinstance(step, dict) else None
            )
            if isinstance(content, str) and not _QUEUED.search(content):
                texts.append(content)
    return Segment(texts, end)


class Scan(NamedTuple):
    """What the transcript gained since an offset.

    ``recheck`` is true when the model's context may have changed: a compaction, or a file
    shorter than the offset (rewritten or rewound). ``end`` is the offset after the last
    complete line read.
    """

    recheck: bool
    end: int


def scan_since(path: str | None, offset: int) -> Scan:
    try:
        if path:
            with Path(path).open("rb") as handle:
                if handle.seek(0, 2) < offset:
                    return Scan(True, offset)
                handle.seek(offset)
                data = handle.read()
            return Scan(CHECKPOINT in data, offset + data.rfind(b"\n") + 1)
    except OSError:
        pass
    return Scan(False, offset)


def _read(path: str | None, offset: int) -> bytes:
    if not path:
        return b""
    try:
        with Path(path).open("rb") as handle:
            handle.seek(offset)
            return handle.read()
    except OSError:
        return b""
