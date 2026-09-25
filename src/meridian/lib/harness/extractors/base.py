"""Typed harness extractor protocol."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generic, Protocol, TypeVar, cast, runtime_checkable

import structlog

from meridian.lib.core.domain import TokenUsage
from meridian.lib.core.native_identity import NativeKey
from meridian.lib.harness.adapter import SpawnExtractor
from meridian.lib.harness.attempt_facts import AttemptFacts
from meridian.lib.harness.common import (
    coerce_optional_float,
    coerce_optional_int,
    iter_nested_dicts,
)
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.launch.launch_types import ResolvedLaunchSpec

ExtractorSpecT = TypeVar("ExtractorSpecT", bound=ResolvedLaunchSpec, covariant=True)


@runtime_checkable
class HarnessExtractor(SpawnExtractor, Protocol, Generic[ExtractorSpecT]):
    """Harness-owned extraction surface shared by subprocess and streaming."""

    def create_fold(self) -> AttemptFold: ...

    def read_native_turn(self, key: NativeKey, turn_ids: tuple[str, ...]) -> str | None:
        return None

    def detect_session_id_from_event(self, event: RawHarnessEvent) -> str | None:
        """Best-effort extraction from one live event frame."""
        ...


def run_event_hooks(
    hooks: Iterable[Callable[[RawHarnessEvent], None]], event: RawHarnessEvent
) -> None:
    """One isolation boundary for facts and side effects, independent of persistence."""
    for hook in hooks:
        try:
            hook(event)
        except Exception:
            structlog.get_logger(__name__).exception("event_hook_failed")


@dataclass
class AttemptFold:
    """One attempt's fold cursor. The registered extractor itself is stateless."""

    extractor: SpawnExtractor
    facts: AttemptFacts = field(default_factory=AttemptFacts)
    scope_session_id: str | None = None
    usage_is_specific: bool = False
    text_source: str | None = None
    generic_usage = True

    def bind_scope(self, session_id: str | None) -> None:
        self.scope_session_id = session_id

    def accepts(self, kind: str, event: RawHarnessEvent) -> bool:
        return True

    def session_id(self, event: RawHarnessEvent) -> str | None:
        return self.extractor.detect_session_id_from_event(event)

    def __call__(self, event: RawHarnessEvent) -> None:
        kind = event.event_type.strip().lower().replace("/", ".")
        self.facts.output_seen = self.facts.output_seen or not kind.startswith("meridian.")
        try:
            if not self.accepts(kind, event):
                return
            self.facts.observe(self.session_id(event))
            if self.generic_usage and not self.usage_is_specific:
                fold_usage_fallback(self.facts, event.payload)
            self.fold_event(kind, event.payload)
        except Exception:
            self.facts.incomplete = True
            raise

    def fold_event(self, kind: str, payload: Mapping[str, object]) -> None:
        raise NotImplementedError

    def set_text(self, text: str, source: str) -> None:
        self.facts.set_text(text)
        self.text_source = source

    def fold_stdout(self, path: Path) -> None:
        """Claude --print capture uses exactly the live fold and isolation boundary."""
        with path.open("rb") as stream:
            for raw_line in stream:
                try:
                    line = raw_line.decode("utf-8")
                except UnicodeDecodeError:
                    self.facts.incomplete = True
                    line = raw_line.decode("utf-8", errors="replace")
                if not line.strip():
                    continue
                self.facts.output_seen = True
                try:
                    payload: object = json.loads(line)
                except ValueError:
                    self.facts.incomplete = True
                    continue
                if isinstance(payload, dict):
                    payload = cast("dict[str, object]", payload)
                    run_event_hooks(
                        (self,),
                        RawHarnessEvent(
                            harness_id="claude",
                            event_type=str(payload.get("type", "")),
                            payload=payload,
                        ),
                    )


def session_from_mapping_with_keys(
    payload: Mapping[str, object],
    keys: tuple[str, ...],
) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for nested in payload.values():
        if isinstance(nested, dict):
            found = session_from_mapping_with_keys(cast("dict[str, object]", nested), keys)
            if found:
                return found
    return None


def normalize_harness_event_type(
    payload: Mapping[str, object],
    *,
    keys: tuple[str, ...] = ("event_type", "event", "type"),
) -> str:
    """Normalize event type fields to a stable dot-separated lowercase value."""

    raw_type: object = ""
    for key in keys:
        if key in payload:
            raw_type = payload.get(key, "")
            break
    return str(raw_type).strip().lower().replace("/", ".")


__all__ = [
    "HarnessExtractor",
    "normalize_harness_event_type",
    "session_from_mapping_with_keys",
]


def fold_usage_fallback(facts: AttemptFacts, event: Mapping[str, object]) -> None:
    """Keep the first best generic live usage until a harness-specific total arrives."""
    usage = facts.usage or TokenUsage()
    for payload in iter_nested_dicts(dict(event)):
        candidate = TokenUsage()
        for input_key, output_key in (
            ("input_tokens", "output_tokens"),
            ("input", "output"),
            ("prompt_tokens", "completion_tokens"),
            ("prompt_token_count", "completion_token_count"),
            ("inputTokenCount", "outputTokenCount"),
        ):
            if input_key in payload or output_key in payload:
                candidate = TokenUsage(
                    input_tokens=coerce_optional_int(payload.get(input_key)),
                    output_tokens=coerce_optional_int(payload.get(output_key)),
                )
                break
        if sum(v is not None for v in (candidate.input_tokens, candidate.output_tokens)) > sum(
            v is not None for v in (usage.input_tokens, usage.output_tokens)
        ):
            usage = usage.model_copy(
                update={
                    "input_tokens": candidate.input_tokens,
                    "output_tokens": candidate.output_tokens,
                }
            )
        if usage.total_cost_usd is None:
            for cost_key in ("total_cost_usd", "cost_usd", "cost", "total_cost", "totalCostUsd"):
                cost = coerce_optional_float(payload.get(cost_key))
                if cost is not None:
                    usage = usage.model_copy(update={"total_cost_usd": cost})
                    break
    if usage != TokenUsage():
        facts.usage = usage
