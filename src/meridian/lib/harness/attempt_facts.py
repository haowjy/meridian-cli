"""Bounded facts of one attempt, folded before delivery; never a second journal."""

from __future__ import annotations

from dataclasses import dataclass, field

from meridian.lib.core.domain import TokenUsage
from meridian.lib.state.permission_journal import PermissionDenial

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
    completion_text: str | None = None
    native_turn_ids: tuple[str, ...] = ()
    usage: TokenUsage | None = None
    failure: HarnessFailure | None = None
    incomplete: bool = False
    text_capped: bool = False
    completion_text_capped: bool = False
    permission_denials: list[PermissionDenial] = field(default_factory=list[PermissionDenial])
    """Calls the harness refused under the profile's policy; finalization journals them."""

    def set_text(self, text: str) -> None:
        encoded = text.encode("utf-8")
        self.text_capped = len(encoded) > _TEXT_LIMIT
        self.final_text = encoded[:_TEXT_LIMIT].decode("utf-8", errors="ignore")

    def observe(self, session_id: str | None) -> None:
        if self.first_session_id is None and session_id:
            self.first_session_id = session_id

    def capture_completion_candidate(self) -> None:
        """Preserve the report that preceded a completion-control turn."""
        self.completion_text = self.final_text
        self.completion_text_capped = self.text_capped

    def clear_completion_candidate(self) -> None:
        """Forget a report whose completion candidate was explicitly rearmed."""
        self.completion_text = None
        self.completion_text_capped = False

    @property
    def report_text_capped(self) -> bool:
        return self.completion_text_capped if self.completion_text is not None else self.text_capped
