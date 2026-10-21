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

from meridian.lib.core.types import HarnessSessionId
from meridian.lib.harness import opencode_transcript
from meridian.lib.state.event_store import utc_now_iso
from meridian.lib.state.session_fold import SessionRecord
from meridian.lib.state.session_store import SessionModelObservationEvent, record_model_observation

_BEST_EFFORT_TAIL_BYTES = 1 << 20


@dataclass(frozen=True)
class NativeModelReadContext:
    """Source-session paths a reader needs to locate the native store."""

    native_store: str | None = None
    project_root: str | None = None
    # Scan only the last N bytes of a JSONL history; a model set earlier is missed.
    tail_bytes: int | None = None


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


def _iter_json_objects(path: Path, tail_bytes: int | None) -> Iterator[dict[str, object]]:
    try:
        with path.open("rb") as handle:
            if tail_bytes is not None:
                size = handle.seek(0, 2)
                if size > tail_bytes:
                    handle.seek(size - tail_bytes)
                    handle.readline()  # drop the partial line the seek landed in
                else:
                    handle.seek(0)
            for raw in handle:
                line = raw.decode("utf-8", errors="ignore")
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
            for payload in _iter_json_objects(native, context.tail_bytes):
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


def observe_session_model(
    runtime_root: Path, record: SessionRecord, *, best_effort: bool = False
) -> str | None:
    """Record the native session's last executed model; return it, or ``None`` if unreadable.

    ``best_effort`` never waits: it reads only the history tail and skips the write
    (raising ``FileLockTimeout``) when an authority lock is contended.
    """

    key = record.native_key()
    if key is None or record.record_mode == "historical":
        return None
    token = read_last_executed_model(
        record.harness,
        key.session_id,
        context=NativeModelReadContext(
            native_store=record.native_store,
            project_root=record.control_root,
            tail_bytes=_BEST_EFFORT_TAIL_BYTES if best_effort else None,
        ),
    )
    if token is not None:
        record_model_observation(
            runtime_root,
            SessionModelObservationEvent(
                harness=record.harness,
                harness_session_id=HarnessSessionId(key.session_id),
                observed_model_token=token,
                recorded_at=utc_now_iso(),
            ),
            lock_timeout=0 if best_effort else None,
        )
    return token


__all__ = [
    "NativeModelReadContext",
    "observe_session_model",
    "read_last_executed_model",
]
