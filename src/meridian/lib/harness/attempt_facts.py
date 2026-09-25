"""Bounded facts of one attempt, folded before delivery; never a second journal."""

from __future__ import annotations

from dataclasses import dataclass

from meridian.lib.core.domain import TokenUsage

_TEXT_LIMIT = 1024 * 1024


@dataclass(frozen=True)
class HarnessFailure:
    message: str
    kind: str


@dataclass
class AttemptFacts:
    first_session_id: str | None = None
    output_seen: bool = False
    final_text: str | None = None
    native_turn_ids: tuple[str, ...] = ()
    usage: TokenUsage | None = None
    failure: HarnessFailure | None = None
    incomplete: bool = False
    text_capped: bool = False

    def set_text(self, text: str) -> None:
        encoded = text.encode("utf-8")
        self.text_capped = len(encoded) > _TEXT_LIMIT
        self.final_text = encoded[:_TEXT_LIMIT].decode("utf-8", errors="ignore")

    def observe(self, session_id: str | None) -> None:
        if self.first_session_id is None and session_id:
            self.first_session_id = session_id
