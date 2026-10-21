"""One parsed transcript snapshot, revealed in contiguous windows as the detail view scrolls."""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

from meridian.cli.browse.text import clip

if TYPE_CHECKING:
    from meridian.lib.ops.session_log import SessionLogEntry
    from meridian.lib.ops.session_transcript import ParsedSessionTranscript

PAGE = 24

type Line = tuple[str, str]  # (style class, text)


class TranscriptWindow:
    """Loaded entries ``[lo, hi)`` of a snapshot; paging never re-reads the source."""

    def __init__(self, parsed: ParsedSessionTranscript, ref: str) -> None:
        self._parsed = parsed
        self._ref = ref
        self.total = len(parsed.all_entries)
        self.lo = self.hi = self.total
        self.entries: list[SessionLogEntry] = []
        self.jump_bottom()

    @classmethod
    def load(cls, chat_id: str, project_root: str) -> TranscriptWindow:
        """Read and parse the whole transcript once; call off the UI thread."""
        from meridian.lib.ops.session_transcript import read_session_transcript

        parsed = read_session_transcript(ref=chat_id, file_path=None, project_root=project_root)
        return cls(parsed, chat_id)

    def _page(
        self, *, before: int | None = None, start: int | None = None
    ) -> list[SessionLogEntry]:
        from meridian.lib.ops.session_log import SessionLogInput, select_session_log_window

        output = select_session_log_window(
            self._parsed,
            SessionLogInput(
                ref=self._ref,
                global_scope=True,
                limit=PAGE,
                before_ordinal=before,
                from_ordinal=start,
            ),
        )
        return [entry for entry in output.entries if entry.kind != "setup"]

    @property
    def has_older(self) -> bool:
        return self.lo > 0

    @property
    def has_newer(self) -> bool:
        return self.hi < self.total

    def older(self) -> int:
        """Prepend the previous page; return how many entries were added."""
        if not self.has_older:
            return 0
        page = self._page(before=self.lo)
        self.lo = min(self.lo, page[0].index) if page else 0
        self.entries[:0] = page
        return len(page)

    def newer(self) -> int:
        if not self.has_newer:
            return 0
        page = self._page(start=self.hi)
        self.hi = page[-1].index + 1 if page else self.total
        self.entries.extend(page)
        return len(page)

    def jump_bottom(self) -> None:
        self.entries = self._page(before=self.total)
        self.hi = self.total
        self.lo = self.entries[0].index if self.entries else self.total

    def jump_top(self) -> None:
        self.entries = self._page(start=0)
        self.lo = 0
        self.hi = self.entries[-1].index + 1 if self.entries else 0


def entry_lines(entry: SessionLogEntry, width: int) -> list[Line]:
    """Role heading, all wrapped content, and a blank separator for one entry.

    Cached per (entry, width) by the caller; the body paints only the visible slice.
    """
    body = max(10, width - 2)
    lines: list[Line] = [(f"class:role-{entry.role}", entry.role)]
    wrapped: list[str] = []
    for raw in entry.content.splitlines() or [""]:
        wrapped.extend(textwrap.wrap(raw, body, drop_whitespace=False) or [""])
    lines.extend(("", f"  {clip(line, body)}") for line in wrapped)
    lines.append(("", ""))
    return lines
