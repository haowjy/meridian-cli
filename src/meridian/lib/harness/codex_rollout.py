"""Codex rollout file discovery helpers."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from meridian.lib.platform import get_home_path
from meridian.lib.platform.atomic import atomic_replace

CODEX_ROLLOUT_FILENAME_RE = re.compile(
    r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-(?P<session_id>[0-9a-fA-F-]{36})\.jsonl$"
)
_TAIL_CHUNK_BYTES = 64 * 1024


def _reverse_lines(path: Path) -> Iterator[bytes]:
    """Yield complete rollout rows newest-first using bounded reads."""

    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        position = handle.tell()
        remainder = b""
        while position:
            read_size = min(position, _TAIL_CHUNK_BYTES)
            position -= read_size
            handle.seek(position)
            block = handle.read(read_size) + remainder
            lines = block.split(b"\n")
            remainder = lines[0]
            yield from reversed(lines[1:])
        if remainder:
            yield remainder


def latest_context_tokens(path: Path) -> int | None:
    """Read Codex's latest trustworthy main-context count from a rollout tail."""

    try:
        for raw_line in _reverse_lines(path):
            if not raw_line.strip():
                continue
            try:
                raw_record: object = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(raw_record, dict):
                continue
            record = cast("dict[str, object]", raw_record)
            if record.get("type") == "compacted":
                return None
            if record.get("type") != "event_msg":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            event = cast("dict[str, object]", payload)
            if event.get("type") != "token_count":
                continue
            info = event.get("info")
            if not isinstance(info, dict):
                continue
            usage = cast("dict[str, object]", info).get("last_token_usage")
            if not isinstance(usage, dict):
                continue
            usage = cast("dict[str, object]", usage)
            input_tokens = usage.get("input_tokens")
            if not isinstance(input_tokens, bool) and input_tokens == 0:
                return None
            total_tokens = usage.get("total_tokens")
            if (
                not isinstance(total_tokens, bool)
                and isinstance(total_tokens, int)
                and total_tokens > 0
            ):
                return total_tokens
    except OSError:
        return None
    return None


class _SessionMetaPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    id: str = Field(min_length=1)


class _SessionMetaHeader(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    type: Literal["session_meta"]
    payload: _SessionMetaPayload


def _rewritten_session_meta(line: bytes, new_session_id: str) -> bytes:
    try:
        payload_obj = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Codex rollout first line is not valid JSON.") from exc
    if not isinstance(payload_obj, dict):
        raise RuntimeError("Codex rollout first line must be a JSON object.")
    payload_dict = cast("dict[str, object]", payload_obj)
    payload = payload_dict.get("payload")
    if payload_dict.get("type") != "session_meta" or not isinstance(payload, dict):
        raise RuntimeError("Codex rollout first line must be a session_meta payload.")
    cast("dict[str, object]", payload)["id"] = new_session_id
    return (json.dumps(payload_dict) + "\n").encode()


def materialize_fork_rollout(*, source_path: Path, target_path: Path, new_session_id: str) -> None:
    """Publish a line-valid snapshot of a possibly live Codex rollout."""

    with source_path.open("rb") as source_handle:
        source_stat = os.fstat(source_handle.fileno())
        remaining = source_stat.st_size
        source_mode = stat.S_IMODE(source_stat.st_mode)
        with atomic_replace(
            target_path,
            mode="wb",
            encoding=None,
            permissions=source_mode,
        ) as target_handle:
            line_number = 0
            while remaining > 0:
                line = source_handle.readline(remaining)
                if not line:
                    break
                remaining -= len(line)
                if not line.endswith(b"\n"):
                    break
                line_number += 1
                if line_number == 1:
                    target_handle.write(_rewritten_session_meta(line, new_session_id))
                    continue
                try:
                    json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise RuntimeError(
                        f"Codex rollout line {line_number} is not valid JSON."
                    ) from exc
                target_handle.write(line)
            if line_number == 0:
                raise RuntimeError(f"Codex rollout has no complete records: {source_path}")


def resolve_codex_home(launch_env: Mapping[str, str]) -> Path:
    codex_home = launch_env.get("CODEX_HOME", "").strip()
    if codex_home:
        return Path(codex_home).expanduser()

    home = launch_env.get("HOME", "").strip()
    if home:
        return Path(home).expanduser() / ".codex"

    return get_home_path() / ".codex"


def resolve_rollout_session_id(
    path: Path,
    project_root: Path,
    *,
    allow_bootstrap_only: bool = False,
) -> str | None:
    session_id: str | None = None
    saw_assistant_message = False
    saw_turn_aborted = False
    resolved_project_root = project_root.resolve()

    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                try:
                    payload_obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload_obj, dict):
                    continue
                payload = cast("dict[str, object]", payload_obj)
                payload_type = payload.get("type")
                if not isinstance(payload_type, str):
                    continue

                if payload_type == "session_meta":
                    raw_meta = payload.get("payload")
                    if not isinstance(raw_meta, dict):
                        continue
                    meta = cast("dict[str, object]", raw_meta)
                    candidate_session_id = meta.get("id")
                    cwd = meta.get("cwd")
                    if (
                        not isinstance(candidate_session_id, str)
                        or not candidate_session_id.strip()
                    ):
                        continue
                    if not isinstance(cwd, str):
                        continue
                    try:
                        if Path(cwd).expanduser().resolve() != resolved_project_root:
                            return None
                    except OSError:
                        continue
                    session_id = candidate_session_id.strip()
                    if allow_bootstrap_only:
                        return session_id
                    continue

                if payload_type == "response_item":
                    raw_item = payload.get("payload")
                    if not isinstance(raw_item, dict):
                        continue
                    item = cast("dict[str, object]", raw_item)
                    if item.get("type") == "message" and item.get("role") == "assistant":
                        saw_assistant_message = True
                    continue

                if payload_type == "event_msg":
                    raw_event = payload.get("payload")
                    if isinstance(raw_event, dict):
                        event_payload = cast("dict[str, object]", raw_event)
                        if event_payload.get("type") == "turn_aborted":
                            saw_turn_aborted = True
                    continue

                if payload_type == "turn_aborted":
                    saw_turn_aborted = True
    except OSError:
        return None

    if session_id is None:
        return None
    if saw_turn_aborted and not saw_assistant_message:
        return None
    return session_id


def find_attachable_rollout_session_id(
    *,
    codex_home: Path,
    project_root: Path,
    session_id: str | None = None,
) -> str | None:
    sessions_root = codex_home / "sessions"
    if not sessions_root.is_dir():
        return None

    normalized_session_id = session_id.strip() if session_id is not None else ""
    if normalized_session_id:
        pattern = f"rollout-*-{normalized_session_id}.jsonl"
    else:
        pattern = "rollout-*.jsonl"
    candidates: list[tuple[float, Path]] = []
    for candidate in sessions_root.rglob(pattern):
        if CODEX_ROLLOUT_FILENAME_RE.match(candidate.name) is None:
            continue
        try:
            modified_at = candidate.stat().st_mtime
        except OSError:
            continue
        candidates.append((modified_at, candidate))

    for _, path in sorted(candidates, key=lambda item: item[0], reverse=True):
        resolved = resolve_rollout_session_id(path, project_root, allow_bootstrap_only=True)
        if resolved is None:
            continue
        if normalized_session_id and resolved != normalized_session_id:
            continue
        return resolved
    return None


def resolve_exact_rollout(session_id: str, matches: list[Path]) -> Path | None:
    """Validate the unique exact-ID candidate, shared by live reads and import."""
    from meridian.lib.core.native_identity import (
        NativeEntryMismatch,
        NativeKeyFields,
        NativeSessionUnavailable,
    )

    if not matches:
        return None

    if len(matches) > 1:
        raise NativeSessionUnavailable(session_id, "ambiguous_native_file")
    source = matches[0]
    try:
        with source.open(encoding="utf-8") as handle:
            header = json.loads(handle.readline())
    except (OSError, UnicodeError, ValueError) as exc:
        raise NativeSessionUnavailable(session_id, "missing") from exc
    try:
        observed = _SessionMetaHeader.model_validate(header).payload.id
    except ValidationError:
        raise NativeSessionUnavailable(session_id, "missing") from None
    if observed != session_id:
        raise NativeEntryMismatch(
            NativeKeyFields("codex", None, session_id),
            NativeKeyFields("codex", None, str(observed)),
        )
    return source


def find_rollout(sessions_root: Path, session_id: str) -> Path | None:
    """Find and validate the unique rollout for an exact Codex session ID."""

    matches = [
        candidate
        for candidate in sessions_root.rglob(f"rollout-*-{session_id}.jsonl")
        if CODEX_ROLLOUT_FILENAME_RE.match(candidate.name) is not None
    ]
    return resolve_exact_rollout(session_id, matches)
