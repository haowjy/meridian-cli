"""Session labels and the `meridian notify` message."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from meridian.plugin_api.types import Notification


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

        description = (
            f"{self.agent} ({self.harness})"
            if self.agent and self.harness
            else self.agent or self.harness or "Meridian"
        )
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


def manual_notification(
    title: str,
    body: str | None,
    label: SessionLabel,
    *,
    priority: int = 3,
    email: bool = True,
    attachments: Sequence[Path] = (),
) -> Notification:
    """Build a `meridian notify` message with its source visible in the title.

    Every attachment must be a readable file now, before any route runs.
    """

    files = tuple(dict.fromkeys(path.expanduser().resolve() for path in attachments))
    for path in files:
        if not path.is_file() or not os.access(path, os.R_OK):
            raise ValueError(f"attachment is not a readable file: {path}")

    title_lines = title.splitlines()
    title_index = next(
        (index for index, line in enumerate(title_lines) if line.strip()),
        None,
    )
    if title_index is None:
        notification_title = title.strip()
        title_detail = ""
    else:
        notification_title = title_lines[title_index].strip()
        title_detail = "\n".join(title_lines[title_index + 1 :]).strip()
    if not notification_title:
        raise ValueError("title must not be empty")

    lines = [title_detail, body.strip() if body else "", label.headline]
    return Notification(
        title=f"{notification_title} — {label.headline}",
        body="\n".join(line for line in lines if line),
        priority=priority,
        email=email,
        label=label.to_dict(),
        attachments=tuple(path.as_posix() for path in files),
    )


__all__ = ["SessionLabel", "manual_notification"]
