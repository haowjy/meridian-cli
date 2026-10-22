"""Session labels and the `meridian notify` message."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from meridian.plugin_api.types import Notification


def actor_name(agent: str | None, harness: str | None) -> str:
    """``agent (harness)``, or whichever is known."""

    if agent and harness:
        return f"{agent} ({harness})"
    return agent or harness or "Meridian"


@dataclass(frozen=True)
class SessionLabel:
    """Who a notification is from: the primary session the human talks to.

    A spawned agent's notification names its primary (terminal, agent, harness,
    work) and records itself in ``child``.
    """

    tmux_session: str | None = None
    project: str | None = None
    work_id: str | None = None
    agent: str | None = None
    harness: str | None = None
    child: str | None = None

    @property
    def sender(self) -> str:
        """Name the terminal and actor: short enough for a notification title."""

        actor = actor_name(self.agent, self.harness)
        return f"{self.tmux_session}: {actor}" if self.tmux_session else actor

    @property
    def headline(self) -> str:
        """The sender, its work item or project, and the child that sent it."""

        if self.work_id:
            line = f'{self.sender} on work: "{self.work_id}"'
        elif self.project:
            line = f'{self.sender} in project: "{self.project}"'
        else:
            line = self.sender
        return f"{line}, from child {self.child}" if self.child else line

    def __str__(self) -> str:
        return self.headline

    def to_dict(self) -> dict[str, str | None]:
        return {
            "tmux_session": self.tmux_session,
            "project": self.project,
            "work_id": self.work_id,
            "agent": self.agent,
            "harness": self.harness,
            "child": self.child,
            "sender": self.sender,
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
        title=f"{label.sender} {notification_title}",
        body="\n".join(line for line in lines if line),
        priority=priority,
        email=email,
        label=label.to_dict(),
        attachments=tuple(path.as_posix() for path in files),
    )


__all__ = ["SessionLabel", "actor_name", "manual_notification"]
