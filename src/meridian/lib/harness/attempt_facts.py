"""Bounded facts of one attempt, folded before delivery; never a second journal."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import structlog

from meridian.lib.core.domain import TokenUsage
from meridian.lib.harness.connections.base import RawHarnessEvent

if TYPE_CHECKING:
    from meridian.lib.harness.adapter import SpawnExtractor

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
    final_text_source: str | None = None
    native_turn_ids: tuple[str, ...] = ()
    usage: TokenUsage | None = None
    usage_is_specific: bool = False
    failure: HarnessFailure | None = None
    incomplete: bool = False
    text_capped: bool = False
    # Only the current owned reply is retained, not every message in the stream.
    scope_session_id: str | None = None
    main_thread_id: str | None = None
    message_id: str | None = None

    def set_text(self, text: str, source: str) -> None:
        encoded = text.encode("utf-8")
        self.text_capped = len(encoded) > _TEXT_LIMIT
        self.final_text = encoded[:_TEXT_LIMIT].decode("utf-8", errors="ignore")
        self.final_text_source = source

    def observe(self, session_id: str | None) -> None:
        if self.first_session_id is None and session_id:
            self.first_session_id = session_id

    def hook(
        self,
        extractor: SpawnExtractor,
        event: RawHarnessEvent,
        *,
        scope_session_id: str | None = None,
    ) -> None:
        """Mark partial facts before letting the emit boundary isolate a fold error."""
        if scope_session_id is not None:
            self.scope_session_id = scope_session_id
        payload = dict(event.payload)
        payload.setdefault("event_type", event.event_type)
        try:
            extractor.fold(self, payload)
        except Exception:
            self.incomplete = True
            raise

    def fold_stdout(self, extractor: SpawnExtractor, path: Path) -> None:
        """Claude --print is black-box capture, not a runner-history reader."""
        with path.open("rb") as stream:
            for raw_line in stream:
                line = raw_line.decode("utf-8", errors="replace")
                if "\ufffd" in line:
                    self.incomplete = True
                self.output_seen = self.output_seen or bool(line.strip())
                try:
                    event: object = json.loads(line)
                except ValueError:
                    self.incomplete = True
                    continue
                if isinstance(event, dict):
                    payload = cast("Mapping[str, object]", event)
                    try:
                        self.hook(
                            extractor,
                            RawHarnessEvent(
                                harness_id="claude",
                                event_type=str(payload.get("type", "")),
                                payload=payload,
                            ),
                        )
                    except Exception:
                        structlog.get_logger(__name__).exception("stdout_fold_failed")
