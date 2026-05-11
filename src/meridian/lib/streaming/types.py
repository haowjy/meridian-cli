"""Types used for streaming control message injection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypedDict


class UserMessageControl(TypedDict):
    """Inject one user message into an active connection."""

    type: Literal["user_message"]
    text: str


class InterruptControl(TypedDict):
    """Interrupt the active runtime turn."""

    type: Literal["interrupt"]


class PermissionReplyControl(TypedDict):
    """Reply to a pending runtime permission request."""

    type: Literal["permission_reply"]
    request_id: str
    decision: str
    payload: dict[str, object]


class UserInputReplyControl(TypedDict):
    """Reply to a pending runtime user-input request."""

    type: Literal["user_input_reply"]
    request_id: str
    answers: dict[str, object]


ControlMessage = (
    UserMessageControl | InterruptControl | PermissionReplyControl | UserInputReplyControl
)


@dataclass(frozen=True)
class InjectResult:
    """Outcome of attempting to inject one control message."""

    success: bool
    inbound_seq: int | None = None
    error: str | None = None


__all__ = [
    "ControlMessage",
    "InjectResult",
    "InterruptControl",
    "PermissionReplyControl",
    "UserInputReplyControl",
    "UserMessageControl",
]
