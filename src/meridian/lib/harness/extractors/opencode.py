"""OpenCode harness extractor."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from meridian.lib.core.domain import TokenUsage
from meridian.lib.core.native_identity import NativeKey
from meridian.lib.harness.common import coerce_optional_float, coerce_optional_int
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.opencode_report import extract_opencode_session_id
from meridian.lib.harness.opencode_snapshot import (
    read_opencode_v1_latest_report,
    read_opencode_v2_turn,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.state.permission_journal import PermissionDenial

from .base import AttemptFold, HarnessExtractor

if TYPE_CHECKING:
    from pydantic import JsonValue


class OpenCodeHarnessExtractor(HarnessExtractor[ResolvedLaunchSpec]):
    """Extractor implementation for OpenCode artifacts and events."""

    def detect_session_id_from_event(self, event: RawHarnessEvent) -> str | None:
        return extract_opencode_session_id(dict(event.payload))

    def create_fold(self, seed: Mapping[str, JsonValue] | None = None) -> AttemptFold:
        return OpenCodeFold(self)

    def read_native_turn(self, key: NativeKey, turn_ids: tuple[str, ...]) -> str | None:
        try:
            return read_opencode_v2_turn(key, turn_ids) or (
                read_opencode_v1_latest_report(key) if not turn_ids else None
            )
        except sqlite3.Error:
            # A missing, busy or corrupt native store cannot invalidate live evidence.
            return None


class OpenCodeFold(AttemptFold):
    message_id: str | None = None
    tool_names: dict[str, str]
    tool_inputs: dict[str, Mapping[str, object]]
    denied_calls: set[str]

    def __init__(self, extractor: OpenCodeHarnessExtractor) -> None:
        super().__init__(extractor)
        self.tool_names = {}
        self.tool_inputs = {}
        self.denied_calls = set()

    def session_id(self, event: RawHarnessEvent) -> str | None:
        session_id = super().session_id(event)
        return session_id if session_id and session_id.startswith("ses_") else None

    def accepts(self, kind: str, event: RawHarnessEvent) -> bool:
        session_id = self.extractor.detect_session_id_from_event(event)
        scope = self.scope_session_id or self.facts.first_session_id
        return not scope or session_id == scope

    def fold_event(self, kind: str, payload: Mapping[str, object]) -> None:
        facts = self.facts
        self._fold_permission_denial(kind, payload)
        if kind in {"session.idle", "message.updated"}:
            usage = _try_parse_opencode_usage(payload)
            if usage is not None:
                self.usage_is_specific = True
                facts.usage = usage
        if kind == "session.text.ended":
            message_id = payload.get("assistantMessageID")
            if isinstance(message_id, str) and message_id:
                facts.native_turn_ids = (message_id,)
            text = payload.get("text")
            if isinstance(text, str) and text:
                self.set_text(text, "opencode_v2_text")
            return
        properties = payload.get("properties")
        if not isinstance(properties, dict):
            return
        properties = cast("dict[str, object]", properties)
        info = properties.get("info")
        if (
            kind == "message.updated"
            and isinstance(info, dict)
            and cast("dict[str, object]", info).get("role") == "assistant"
        ):
            info = cast("dict[str, object]", info)
            message_id = info.get("id")
            if isinstance(message_id, str) and message_id != self.message_id:
                self.message_id = message_id
                facts.final_text = None
                self.text_source = None
            parts = info.get("parts")
            if isinstance(parts, list):
                text = "".join(
                    str(cast("dict[str, object]", part).get("text", ""))
                    for part in cast("list[object]", parts)
                    if isinstance(part, dict)
                    and cast("dict[str, object]", part).get("type") == "text"
                )
                if text.strip() and self.text_source != "opencode_v1_parts":
                    self.set_text(text.strip(), "opencode_v1_embedded")
        part = properties.get("part")
        if (
            kind == "message.part.updated"
            and isinstance(part, dict)
            and cast("dict[str, object]", part).get("type") == "text"
            and self.message_id
            and cast("dict[str, object]", part).get(
                "messageID", cast("dict[str, object]", part).get("message_id")
            )
            == self.message_id
        ):
            text = cast("dict[str, object]", part).get("text")
            if isinstance(text, str) and text.strip():
                prior = facts.final_text if self.text_source == "opencode_v1_parts" else None
                self.set_text((prior or "") + text.strip(), "opencode_v1_parts")

    def _fold_permission_denial(self, kind: str, payload: Mapping[str, object]) -> None:
        """Capture native V1/V2 permission failures that reach the event stream."""

        if kind == "session.tool.input.started":
            request_id = payload.get("id")
            tool = payload.get("name")
            if isinstance(request_id, str) and isinstance(tool, str):
                self.tool_names[request_id] = tool
            return
        if kind == "session.tool.called":
            request_id = payload.get("id")
            input_value = payload.get("input")
            if isinstance(request_id, str) and isinstance(input_value, dict):
                self.tool_inputs[request_id] = cast("dict[str, object]", input_value)
            return
        if kind == "session.tool.failed":
            error = payload.get("error")
            if not _is_permission_error(error):
                return
            request_id = payload.get("id")
            if not isinstance(request_id, str):
                return
            self._record_permission_denial(
                request_id=request_id,
                tool=self.tool_names.get(request_id, "tool"),
                input_value=self.tool_inputs.get(request_id, {}),
                error=error,
            )
            return
        if kind != "message.part.updated":
            return
        properties = payload.get("properties")
        if not isinstance(properties, dict):
            return
        part = cast("dict[str, object]", properties).get("part")
        if not isinstance(part, dict):
            return
        part = cast("dict[str, object]", part)
        state = part.get("state")
        if not isinstance(state, dict):
            return
        state = cast("dict[str, object]", state)
        error = state.get("error")
        if state.get("status") != "error" or not _is_permission_error(error):
            return
        request_id = part.get("callID") or part.get("id")
        if not isinstance(request_id, str):
            return
        input_value = state.get("input")
        self._record_permission_denial(
            request_id=request_id,
            tool=str(part.get("tool") or "tool"),
            input_value=cast("dict[str, object]", input_value)
            if isinstance(input_value, dict)
            else {},
            error=error,
        )

    def _record_permission_denial(
        self,
        *,
        request_id: str,
        tool: str,
        input_value: Mapping[str, object],
        error: object,
    ) -> None:
        if request_id in self.denied_calls:
            return
        self.denied_calls.add(request_id)
        self.facts.permission_denials.append(
            PermissionDenial(
                request_id=request_id,
                tool=tool,
                detail=_permission_detail(input_value),
                reason=_permission_reason(error),
            )
        )


OPENCODE_EXTRACTOR = OpenCodeHarnessExtractor()

__all__ = ["OPENCODE_EXTRACTOR", "OpenCodeHarnessExtractor"]


def _is_permission_error(error: object) -> bool:
    def v1_rule_denial(message: object) -> bool:
        normalized = str(message).lower()
        return "specified a rule" in normalized and "prevents" in normalized

    if isinstance(error, dict):
        error = cast("dict[str, object]", error)
        return error.get("type") == "permission.rejected"
    return v1_rule_denial(error or "")


def _permission_reason(error: object) -> str:
    if isinstance(error, dict):
        message = cast("dict[str, object]", error).get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    if isinstance(error, str) and error.strip():
        return error.strip()
    return "OpenCode denied the tool call."


def _permission_detail(input_value: Mapping[str, object]) -> str:
    for key in ("command", "path", "filePath", "file_path"):
        value = input_value.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return json.dumps(dict(input_value), sort_keys=True, separators=(",", ":"), default=str)


def _try_parse_opencode_usage(payload: Mapping[str, object]) -> TokenUsage | None:
    properties_obj = payload.get("properties")
    properties = (
        cast("dict[str, object]", properties_obj) if isinstance(properties_obj, dict) else None
    )
    nested_info_obj = properties.get("info") if properties is not None else None
    nested_info = (
        cast("dict[str, object]", nested_info_obj) if isinstance(nested_info_obj, dict) else None
    )
    nested_tokens_obj = nested_info.get("tokens") if nested_info is not None else None
    nested_tokens = (
        cast("dict[str, object]", nested_tokens_obj)
        if isinstance(nested_tokens_obj, dict)
        else None
    )
    if nested_tokens is not None:
        nested_cache_obj = nested_tokens.get("cache")
        nested_cache = (
            cast("dict[str, object]", nested_cache_obj)
            if isinstance(nested_cache_obj, dict)
            else {}
        )
        nested_usage = TokenUsage(
            input_tokens=coerce_optional_int(nested_tokens.get("input")),
            output_tokens=coerce_optional_int(nested_tokens.get("output")),
            cache_read_input_tokens=coerce_optional_int(nested_cache.get("read")),
            cache_creation_input_tokens=coerce_optional_int(nested_cache.get("write")),
            reasoning_tokens=coerce_optional_int(nested_tokens.get("reasoning")),
            total_cost_usd=coerce_optional_float(
                nested_info.get("cost") if nested_info is not None else None
            ),
        )
        if any(
            value is not None
            for value in (
                nested_usage.input_tokens,
                nested_usage.output_tokens,
                nested_usage.cache_read_input_tokens,
                nested_usage.cache_creation_input_tokens,
                nested_usage.reasoning_tokens,
            )
        ):
            return nested_usage

    info_obj = payload.get("info")
    info = cast("dict[str, object]", info_obj) if isinstance(info_obj, dict) else payload
    usage_obj = payload.get("usage")
    usage_source = cast("dict[str, object]", usage_obj) if isinstance(usage_obj, dict) else info
    usage = usage_source
    cost_obj = payload.get("cost")
    cost_source = cast("dict[str, object]", cost_obj) if isinstance(cost_obj, dict) else payload
    legacy_usage = TokenUsage(
        input_tokens=coerce_optional_int(usage.get("input_tokens") or usage.get("input")),
        output_tokens=coerce_optional_int(usage.get("output_tokens") or usage.get("output")),
        cache_read_input_tokens=coerce_optional_int(
            usage.get("cache_read_input_tokens") or usage.get("cache_read")
        ),
        cache_creation_input_tokens=coerce_optional_int(
            usage.get("cache_creation_input_tokens") or usage.get("cache_write")
        ),
        reasoning_tokens=coerce_optional_int(
            usage.get("reasoning_tokens") or usage.get("reasoning")
        ),
        total_cost_usd=coerce_optional_float(cost_source.get("total_cost_usd")),
    )
    if any(
        value is not None
        for value in (
            legacy_usage.input_tokens,
            legacy_usage.output_tokens,
            legacy_usage.cache_read_input_tokens,
            legacy_usage.cache_creation_input_tokens,
            legacy_usage.reasoning_tokens,
        )
    ):
        return legacy_usage
    return None
