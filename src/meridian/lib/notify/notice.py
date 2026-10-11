"""Notification values shared by callers and delivery channels."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionLabel:
    """Optional launch context rendered for notification surfaces."""

    tmux_session: str | None = None
    project: str | None = None
    work_id: str | None = None
    agent: str | None = None
    harness: str | None = None

    @property
    def headline(self) -> str:
        """Name the terminal, actor, and explicitly labelled work or project."""

        description = self.agent or self.harness or "Meridian"
        if self.work_id:
            description += f' on work: "{self.work_id}"'
        elif self.project:
            description += f' in project: "{self.project}"'
        return f"{self.tmux_session}: {description}" if self.tmux_session else description

    def __str__(self) -> str:
        return self.headline

    def to_dict(self) -> dict[str, str | None]:
        return {
            "tmux_session": self.tmux_session,
            "project": self.project,
            "work_id": self.work_id,
            "agent": self.agent,
            "harness": self.harness,
            "headline": self.headline,
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


def manual_notice(
    message: str,
    label: SessionLabel,
    *,
    title: str | None = None,
    priority: int = 3,
    email: bool = True,
) -> Notice:
    """Build a manual notice while preserving its launch description."""

    normalized_title = title.strip() if title and title.strip() else None
    body_lines = [message]
    if normalized_title is not None:
        body_lines.append(label.headline)
    return Notice(
        title=normalized_title or label.headline,
        body="\n".join(line for line in body_lines if line),
        priority=priority,
        email=email,
        kind="manual",
    )


__all__ = ["Notice", "SessionLabel", "manual_notice"]
