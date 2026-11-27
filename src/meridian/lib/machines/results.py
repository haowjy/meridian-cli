"""What a call to another machine returns. ``call_machine`` prints nothing."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NotSent:
    """Nothing reached the other machine's command: safe to run again. *kind* is a message
    error kind (``unreachable``, ``auth``, ``host_key``, ``version``, ``refused``, ``failed``)."""

    kind: str
    text: str


@dataclass(frozen=True)
class Unanswered:
    """The request was written, in whole or in part, and no whole result line came back."""

    text: str


@dataclass(frozen=True)
class Answered:
    exit: int
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool
    machine: str
    id: str
    version: str


type CallResult = NotSent | Unanswered | Answered

__all__ = ["Answered", "CallResult", "NotSent", "Unanswered"]
