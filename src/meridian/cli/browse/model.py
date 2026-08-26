"""Pure state machine for the session browser."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from meridian.lib.ops.session_list import SessionListRow

type BrowseMode = Literal["list", "search-input", "searching"]


@dataclass(frozen=True)
class Character:
    value: str


@dataclass(frozen=True)
class Move:
    delta: int


@dataclass(frozen=True)
class Backspace:
    pass


@dataclass(frozen=True)
class Search:
    pass


@dataclass(frozen=True)
class Enter:
    pass


@dataclass(frozen=True)
class Escape:
    pass


@dataclass(frozen=True)
class Interrupt:
    pass


type Key = Character | Move | Backspace | Search | Enter | Escape | Interrupt


@dataclass(frozen=True)
class Activate:
    chat_id: str


@dataclass(frozen=True)
class StartSearch:
    query: str
    chat_ids: tuple[str, ...]


@dataclass(frozen=True)
class Quit:
    exit_code: int = 0


type Command = Activate | StartSearch | Quit


def _subsequence(needle: str, haystack: str) -> bool:
    characters = iter(haystack)
    return all(any(candidate == wanted for candidate in characters) for wanted in needle)


@dataclass
class BrowseModel:
    rows: tuple[SessionListRow, ...]
    older_count: int = 0
    filter_text: str = ""
    mode: BrowseMode = "list"
    highlight: int = 0
    search_query: str = ""
    search_scanned: int = 0
    search_total: int = 0
    search_matches: frozenset[str] | None = None
    preview_chat_id: str | None = None
    preview_lines: tuple[str, ...] = ()
    preview_loading: bool = False
    inline_message: str | None = None

    @property
    def visible_rows(self) -> tuple[SessionListRow, ...]:
        needle = self.filter_text.strip().lower()
        rows = self.rows
        if needle:
            rows = tuple(
                row
                for row in rows
                if needle in row.filter_text or _subsequence(needle, row.filter_text)
            )
        if self.search_matches is not None:
            rows = tuple(row for row in rows if row.chat_id in self.search_matches)
        return rows

    @property
    def highlighted_row(self) -> SessionListRow | None:
        rows = self.visible_rows
        if not rows:
            return None
        return rows[min(self.highlight, len(rows) - 1)]

    def _clamp(self) -> None:
        self.highlight = max(0, min(self.highlight, max(0, len(self.visible_rows) - 1)))

    def _clear_inline(self) -> None:
        self.inline_message = None

    def handle_key(self, key: Key) -> Command | None:
        if isinstance(key, Interrupt):
            return Quit(130)
        if isinstance(key, Escape):
            if self.mode != "list" or self.search_matches is not None:
                self.mode = "list"
                self.search_query = ""
                self.search_scanned = 0
                self.search_total = 0
                self.search_matches = None
                self.highlight = 0
                self._clear_inline()
                self._clamp()
                return None
            return Quit()
        if isinstance(key, Search):
            if self.mode == "list":
                self.mode = "search-input"
                self.search_query = ""
                self.search_matches = None
                self._clear_inline()
            elif self.mode == "search-input":
                self.search_query += "/"
            return None
        if isinstance(key, Move):
            if self.visible_rows:
                self.highlight += key.delta
                self._clamp()
                self._clear_inline()
            return None
        if isinstance(key, Backspace):
            if self.mode == "search-input":
                self.search_query = self.search_query[:-1]
            elif self.mode == "list":
                self.filter_text = self.filter_text[:-1]
                self.highlight = 0
                self._clear_inline()
                self._clamp()
            return None
        if isinstance(key, Character):
            if not key.value.isprintable() or len(key.value) != 1:
                return None
            if self.mode == "searching":
                return None
            if self.mode == "search-input":
                self.search_query += key.value
                return None
            if not self.rows:
                return Quit() if key.value == "q" else None
            if key.value == "q" and not self.filter_text:
                return Quit()
            self.filter_text += key.value
            self.highlight = 0
            self._clear_inline()
            self._clamp()
            return None
        if self.mode == "search-input":
            query = self.search_query.strip()
            if not query:
                return None
            chat_ids = tuple(row.chat_id for row in self.visible_rows)
            self.mode = "searching"
            self.search_scanned = 0
            self.search_total = len(chat_ids)
            self.search_matches = None
            self._clear_inline()
            return StartSearch(query, chat_ids)
        row = self.highlighted_row
        return Activate(row.chat_id) if row is not None else None

    def apply_preview(self, chat_id: str, lines: tuple[str, ...]) -> None:
        if self.highlighted_row is None or self.highlighted_row.chat_id != chat_id:
            return
        self.preview_chat_id = chat_id
        self.preview_lines = lines
        self.preview_loading = False

    def apply_search_progress(self, scanned: int, total: int) -> None:
        if self.mode != "searching":
            return
        self.search_scanned = scanned
        self.search_total = total

    def apply_search_done(self, matched_chat_ids: frozenset[str], total: int) -> None:
        if self.mode != "searching":
            return
        self.search_scanned = total
        self.search_total = total
        self.search_matches = matched_chat_ids
        self.highlight = 0
        self._clamp()

    def apply_blocked(self, chat_id: str, reason: str) -> None:
        self.inline_message = f"{chat_id}: {reason}"


__all__ = [
    "Activate",
    "Backspace",
    "BrowseModel",
    "Character",
    "Command",
    "Enter",
    "Escape",
    "Interrupt",
    "Key",
    "Move",
    "Quit",
    "Search",
    "StartSearch",
]
