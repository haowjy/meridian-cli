"""Notification values shared by callers and delivery channels."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


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

    @classmethod
    def from_dict(cls, values: Mapping[str, str | None]) -> SessionLabel:
        return cls(
            tmux_session=values.get("tmux_session"),
            project=values.get("project"),
            work_id=values.get("work_id"),
            agent=values.get("agent"),
            harness=values.get("harness"),
        )


@dataclass(frozen=True)
class Notice:
    """One message, the hook event it travels as, and its delivery policy."""

    title: str
    body: str
    priority: int
    email: bool
    # The hook event that carries it: session.responded, session.cache_expiring, notify.manual.
    event: str
    label: SessionLabel | None = None
    # Files each transport sends as it can: email attaches them all, ntfy one per message.
    attachments: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        if not 1 <= self.priority <= 5:
            raise ValueError(f"priority must be between 1 and 5, got {self.priority}")


def manual_notice(
    title: str,
    body: str | None,
    label: SessionLabel,
    *,
    priority: int = 3,
    email: bool = True,
    attachments: Sequence[Path] = (),
) -> Notice:
    """Build a manual notice; its body always ends with where it came from.

    Every attachment must be a readable file now, before any transport runs.
    """

    normalized_title = title.strip()
    if not normalized_title:
        raise ValueError("title must not be empty")
    files = tuple(path.expanduser().resolve() for path in attachments)
    for path in files:
        if not path.is_file() or not os.access(path, os.R_OK):
            raise ValueError(f"attachment is not a readable file: {path}")
    lines = [body.strip() if body else "", label.headline]
    return Notice(
        title=normalized_title,
        body="\n".join(line for line in lines if line),
        priority=priority,
        email=email,
        event="notify.manual",
        label=label,
        attachments=files,
    )


__all__ = ["Notice", "SessionLabel", "manual_notice"]
