"""Bounded preview accumulation over the canonical transcript normalizer."""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict

from meridian.lib.harness.transcript import (
    DefaultTranscriptEventParser,
    TranscriptNormalizer,
)

_MESSAGE_BYTES = 16 * 1024
_SETUP_BYTES = 2 * 1024


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


class TranscriptPreview(BaseModel):
    """Serializable bounded parser checkpoint, never authoritative conversation state."""

    model_config = ConfigDict(frozen=True)
    version: int = 2
    messages: tuple[PreviewMessage, ...] = ()
    setup: str | None = None
    pending_summary: str | None = None
    has_interaction: bool = False
    omitted_messages: bool = False
    clipped_text: bool = False

    def lines(self) -> tuple[str, ...]:
        lines: list[str] = []
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
        self.normalizer = TranscriptNormalizer(self.preview.setup, self.preview.pending_summary)
        self.parser = DefaultTranscriptEventParser()

    def feed(self, event: dict[str, object]) -> None:
        normalized = self.normalizer.feed(event, self.parser)
        messages = [] if normalized.boundary else list(self.preview.messages)
        clipped = False if normalized.boundary else self.preview.clipped_text
        omitted = False if normalized.boundary else self.preview.omitted_messages
        has_interaction = self.preview.has_interaction
        for message in normalized.messages:
            if message.role not in {"assistant", "user"} or not message.content.strip():
                continue
            has_interaction = True
            content = _clip(message.content, _MESSAGE_BYTES)
            clipped |= content != message.content
            messages.append(
                PreviewMessage(
                    role=message.role,
                    content=content,
                    tool=_clip(message.tool_call.name, 128) if message.tool_call else None,
                    is_tool_result=message.is_tool_result,
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
            has_interaction=has_interaction,
            clipped_text=clipped,
            omitted_messages=omitted,
        )
