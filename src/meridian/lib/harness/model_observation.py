"""Native-history read seam for the last executed conversation model.

Harness-specific session stores are read here to recover the model a harness
actually last executed — as opposed to the model Meridian selected at startup.
Every reader returns a routable model token (verbatim, never remapped) or ``None``;
unknown harnesses, missing stores, unparseable data, and I/O errors never raise.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from meridian.lib.harness import opencode_transcript


@dataclass(frozen=True)
class NativeModelReadContext:
    """Source-session paths a reader needs to locate the native store."""

    native_store: str | None = None
    project_root: str | None = None


def _nested_str(payload: object, *keys: str) -> str | None:
    current: object = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = cast("dict[str, object]", current).get(key)
    if isinstance(current, str):
        stripped = current.strip()
        if stripped:
            return stripped
    return None


def _iter_json_objects(path: Path) -> Iterator[dict[str, object]]:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    yield cast("dict[str, object]", payload)
    except OSError:
        return


def read_last_executed_model(
    harness: str,
    harness_session_id: str,
    *,
    context: NativeModelReadContext,
) -> str | None:
    """Return the last executed model token for a native session, or None.

    Never raises: unknown harnesses, missing stores, unparseable data, and any
    ``OSError``/``sqlite3.Error`` all surface as ``None``.
    """

    normalized_session_id = harness_session_id.strip()
    if not normalized_session_id or harness not in {"claude", "codex", "opencode", "pi"}:
        return None
    try:
        if context.native_store is not None:
            from meridian.lib.core.types import HarnessId
            from meridian.lib.harness.registry import get_default_harness_registry

            adapter = get_default_harness_registry().get(HarnessId(harness))
            native = adapter.resolve_native_session_file(
                project_root=Path(context.project_root or "."),
                session_id=normalized_session_id,
                native_store=Path(context.native_store),
            )
            if native is None:
                return None
            if harness == "opencode":
                return opencode_transcript.read_last_model(
                    normalized_session_id, launch_env={"OPENCODE_DB": str(native)}
                )
            last: str | None = None
            fallback: str | None = None
            for payload in _iter_json_objects(native):
                event_type = payload.get("type")
                if harness == "claude" and event_type == "assistant":
                    last = _nested_str(payload, "message", "model") or last
                elif harness == "codex":
                    if event_type == "turn_context":
                        last = _nested_str(payload, "payload", "model") or last
                    elif event_type == "world_state":
                        fallback = _nested_str(payload, "payload", "state", "model") or fallback
                elif harness == "pi":
                    if event_type == "model_change":
                        last = _nested_str(payload, "modelId") or last
                    elif event_type == "session":
                        fallback = _nested_str(payload, "model") or _nested_str(
                            payload, "model", "modelId"
                        ) or fallback
            return last or fallback
    except (OSError, ValueError, RuntimeError, sqlite3.Error):
        return None
    return None


__all__ = [
    "NativeModelReadContext",
    "read_last_executed_model",
]
