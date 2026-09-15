"""Bounded preview accumulation over the canonical transcript normalizer."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict

from meridian.lib.harness.transcript import (
    DefaultTranscriptEventParser,
    TranscriptNormalizer,
)

_MESSAGE_BYTES = 16 * 1024
_SETUP_BYTES = 2 * 1024
TRANSCRIPT_PREVIEW_VERSION = 3


def _clip(text: str, limit: int) -> str:
    encoded = text.encode("utf-8")
    if len(json.dumps(text, ensure_ascii=False).encode("utf-8")) <= limit:
        return text
    encoded = encoded[-max(0, limit - 32) :]
    while (
        len(
            json.dumps(encoded.decode("utf-8", errors="ignore"), ensure_ascii=False).encode("utf-8")
        )
        > limit - 32
    ):
        encoded = encoded[len(encoded) // 2 :]
    return "[…clipped…] " + encoded.decode("utf-8", errors="ignore")


class PreviewMessage(BaseModel):
    model_config = ConfigDict(frozen=True)
    role: str
    content: str
    tool: str | None = None
    is_tool_result: bool = False
    kind: Literal["interaction", "annotation"] = "interaction"


class TranscriptPreview(BaseModel):
    """Serializable bounded parser checkpoint, never authoritative conversation state."""

    model_config = ConfigDict(frozen=True)
    version: int = TRANSCRIPT_PREVIEW_VERSION
    messages: tuple[PreviewMessage, ...] = ()
    setup: str | None = None
    pending_summary: str | None = None
    pi_session: bool = False
    pi_previous_entry_id: str | None = None
    rendering_reason: str | None = None
    has_interaction: bool = False
    omitted_messages: bool = False
    clipped_text: bool = False

    def lines(self) -> tuple[str, ...]:
        lines: list[str] = []
        if self.rendering_reason:
            lines.append(self.rendering_reason)
        if self.setup:
            lines.extend(("setup:", *self.setup.splitlines()))
        for message in self.messages:
            role = "tool result" if message.is_tool_result else message.role
            label = f"{role} · {message.tool}" if message.tool else role
            lines.extend((f"{label}:", *message.content.splitlines()))
        return tuple(lines) or ("No messages in the current segment.",)


class PreviewAccumulator:
    """Retain recent messages while sharing all setup/compaction interpretation."""

    def __init__(self, checkpoint: TranscriptPreview | None = None) -> None:
        self.preview = checkpoint or TranscriptPreview()
        self.normalizer = TranscriptNormalizer(
            setup=self.preview.setup,
            pending_summary=self.preview.pending_summary,
            pi_session=self.preview.pi_session,
            pi_previous_entry_id=self.preview.pi_previous_entry_id,
            rendering_reason=self.preview.rendering_reason,
        )
        self.parser = DefaultTranscriptEventParser()

    def feed(self, event: dict[str, object]) -> None:
        normalized = self.normalizer.feed(event, self.parser)
        messages = [] if normalized.boundary else list(self.preview.messages)
        clipped = False if normalized.boundary else self.preview.clipped_text
        omitted = False if normalized.boundary else self.preview.omitted_messages
        has_interaction = self.preview.has_interaction
        for message in normalized.messages:
            if (
                message.role not in {"assistant", "user", "annotation"}
                or not message.content.strip()
            ):
                continue
            has_interaction |= message.kind == "interaction"
            content = _clip(message.content, _MESSAGE_BYTES)
            clipped |= content != message.content
            messages.append(
                PreviewMessage(
                    role=message.role,
                    content=content,
                    tool=_clip(message.tool_call.name, 128) if message.tool_call else None,
                    is_tool_result=message.is_tool_result,
                    kind=message.kind,
                )
            )
            while (
                len(messages) > 10
                or sum(
                    len(json.dumps(m.content, ensure_ascii=False).encode("utf-8")) for m in messages
                )
                > _MESSAGE_BYTES
            ):
                messages.pop(0)
                omitted = True
        setup = _clip(self.normalizer.setup, _SETUP_BYTES) if self.normalizer.setup else None
        clipped |= setup != self.normalizer.setup
        # Only setup presence affects subsequent parsing, not its unbounded text.
        self.normalizer.setup = setup
        self.preview = TranscriptPreview(
            messages=tuple(messages),
            setup=setup,
            pending_summary=self.normalizer.pending_summary,
            pi_session=self.normalizer.pi_session,
            pi_previous_entry_id=self.normalizer.pi_previous_entry_id,
            rendering_reason=self.normalizer.rendering_reason,
            has_interaction=has_interaction,
            clipped_text=clipped,
            omitted_messages=omitted,
        )
