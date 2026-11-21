"""What ``agy``'s model context holds, read from the conversation's transcript.

``agy`` has no compaction hook (its plugin events are ``SessionStart``, ``SessionEnd``,
``PreInvocation``, ``PostInvocation``, ``PreToolUse``, ``PostToolUse``, ``UserPromptSubmit``
and ``Stop``), and a compaction keeps neither of Meridian's injected blocks. The model's
context is the transcript's steps after its last ``CHECKPOINT`` (``agy`` 1.5.14 appends;
nothing earlier is rewritten), so a block is held when an injected message after that step
carries it.

``guard.sh`` greps the bytes after a recorded offset for ``CHECKPOINT``; keep ``CHECKPOINT``
here and there the same.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple, cast

CHECKPOINT = b'"type":"CHECKPOINT"'
_SYSTEM_MESSAGE = b'"type":"SYSTEM_MESSAGE"'


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
            if isinstance(content, str):
                texts.append(content)
    return Segment(texts, end)


def compacted_since(path: str | None, offset: int) -> bool:
    return CHECKPOINT in _read(path, offset)


def _read(path: str | None, offset: int) -> bytes:
    if not path:
        return b""
    try:
        with Path(path).open("rb") as handle:
            handle.seek(offset)
            return handle.read()
    except OSError:
        return b""
