"""Notification values shared by callers and delivery channels."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionLabel:
    """Optional session context rendered as one notification label."""

    tmux_session: str | None = None
    project: str | None = None
    work_id: str | None = None

    @property
    def text(self) -> str:
        context = " · ".join(part for part in (self.project, self.work_id) if part)
        session = f"[{self.tmux_session}]" if self.tmux_session else ""
        return " ".join(part for part in (session, context) if part)

    def __str__(self) -> str:
        return self.text

    def titled(self, title: str | None = None) -> str:
        """Combine a user title with this label without losing session context."""
        parts = [part for part in (self.text, title.strip() if title else None) if part]
        return " · ".join(parts) if parts else "Meridian"

    def to_dict(self) -> dict[str, str | None]:
        return {
            "tmux_session": self.tmux_session,
            "project": self.project,
            "work_id": self.work_id,
            "text": self.text,
        }


@dataclass(frozen=True)
class Notice:
    """One message and its delivery policy."""

    title: str
    body: str
    priority: int
    email: bool
    kind: str

    def __post_init__(self) -> None:
        if not 1 <= self.priority <= 5:
            raise ValueError(f"priority must be between 1 and 5, got {self.priority}")
