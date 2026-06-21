"""Claude on-disk session and history helpers."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import cast

from meridian.lib.platform import get_home_path

logger = logging.getLogger(__name__)


def project_slug(project_root: Path) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", str(project_root.resolve()))


def _claude_config_root() -> Path:
    configured_root = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if configured_root:
        return Path(configured_root).expanduser().resolve()
    return get_home_path() / ".claude"


def _claude_projects_root() -> Path:
    return _claude_config_root() / "projects"


def _claude_history_path() -> Path:
    return _claude_config_root() / "history.jsonl"


def _claude_project_dir(project_root: Path) -> Path:
    return _claude_projects_root() / project_slug(project_root)


def candidate_claude_project_dirs(project_root: Path) -> list[Path]:
    """Return the exact Claude project directory for this project root."""
    return [_claude_projects_root() / project_slug(project_root)]


def _read_claude_session_id(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            first_line = handle.readline().strip()
    except OSError:
        logger.debug("Failed to read Claude session file %s", path, exc_info=True)
        return None
    if not first_line:
        return None
    try:
        payload = json.loads(first_line)
    except json.JSONDecodeError:
        return path.stem.strip() or None
    if not isinstance(payload, dict):
        return path.stem.strip() or None
    payload_dict = cast("dict[str, object]", payload)
    session_id = payload_dict.get("sessionId")
    if isinstance(session_id, str) and session_id.strip():
        return session_id.strip()
    return path.stem.strip() or None


def _same_claude_history_project(raw_project: object, project_root: Path) -> bool:
    if not isinstance(raw_project, str) or not raw_project.strip():
        return False
    try:
        return Path(raw_project).expanduser().resolve() == project_root.resolve()
    except OSError:
        return False


def _extract_history_timestamp(payload: dict[str, object]) -> float | None:
    raw_timestamp = payload.get("timestamp")
    if isinstance(raw_timestamp, bool) or raw_timestamp is None:
        return None
    if isinstance(raw_timestamp, int | float):
        timestamp = float(raw_timestamp)
    elif isinstance(raw_timestamp, str):
        try:
            timestamp = float(raw_timestamp.strip())
        except ValueError:
            try:
                normalized_timestamp = raw_timestamp.strip()
                if normalized_timestamp.endswith("Z"):
                    normalized_timestamp = f"{normalized_timestamp[:-1]}+00:00"
                return datetime.fromisoformat(normalized_timestamp).timestamp()
            except ValueError:
                return None
    else:
        return None
    if timestamp > 10_000_000_000:
        return timestamp / 1000
    return timestamp


def _message_content_text(raw_content: object) -> str:
    if isinstance(raw_content, str):
        return raw_content.strip()
    if not isinstance(raw_content, list):
        return ""
    parts: list[str] = []
    for item in cast("list[object]", raw_content):
        if not isinstance(item, dict):
            continue
        item_dict = cast("dict[str, object]", item)
        text = item_dict.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n".join(parts).strip()


def _first_user_prompt_matches_history(
    *,
    transcript_path: Path,
    history_display: str,
    history_timestamp: float | None,
) -> bool:
    normalized_display = " ".join(history_display.split())
    if not normalized_display:
        return False
    try:
        with transcript_path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    payload_obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload_obj, dict):
                    continue
                payload = cast("dict[str, object]", payload_obj)
                message = payload.get("message")
                if isinstance(message, dict):
                    message_dict = cast("dict[str, object]", message)
                    role = str(message_dict.get("role") or "").strip()
                    content = _message_content_text(message_dict.get("content"))
                else:
                    role = str(payload.get("type") or "").strip()
                    content = _message_content_text(payload.get("content"))
                if role != "user":
                    continue
                normalized_content = " ".join(content.split())
                if normalized_content != normalized_display:
                    return False
                transcript_timestamp = _extract_history_timestamp(payload)
                return history_timestamp is None or (
                    transcript_timestamp is not None
                    and abs(transcript_timestamp - history_timestamp) <= 1
                )
    except OSError:
        logger.debug("Failed to read Claude transcript file %s", transcript_path, exc_info=True)
    return False


def _valid_successor_transcript(
    *,
    project_dir: Path,
    session_id: str,
    history_display: str,
    history_timestamp: float | None,
    started_at_epoch: float | None,
) -> bool:
    transcript_path = project_dir / f"{session_id}.jsonl"
    try:
        if not transcript_path.is_file():
            return False
        if started_at_epoch is not None and transcript_path.stat().st_mtime + 1 < started_at_epoch:
            return False
    except OSError:
        return False
    return _read_claude_session_id(
        transcript_path
    ) == session_id and _first_user_prompt_matches_history(
        transcript_path=transcript_path,
        history_display=history_display,
        history_timestamp=history_timestamp,
    )


def _find_tui_trampoline_successor_session_id(
    *,
    project_root: Path,
    recorded_session_id: str,
    started_at_epoch: float | None,
) -> str | None:
    history_path = _claude_history_path()
    project_dir = _claude_project_dir(project_root)
    if not history_path.is_file() or not project_dir.is_dir():
        return None

    looking_for_successor = False
    trampoline_timestamp: float | None = None
    prior_same_project_session_ids: set[str] = set()
    candidate_session_id: str | None = None
    try:
        with history_path.open("r", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    payload_obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload_obj, dict):
                    continue
                payload = cast("dict[str, object]", payload_obj)
                if not _same_claude_history_project(payload.get("project"), project_root):
                    continue
                session_id = str(payload.get("sessionId") or "").strip()
                display = str(payload.get("display") or "").strip()
                if not session_id:
                    continue
                if session_id == recorded_session_id and display == "/tui fullscreen":
                    looking_for_successor = True
                    trampoline_timestamp = _extract_history_timestamp(payload)
                    continue
                if not looking_for_successor:
                    prior_same_project_session_ids.add(session_id)
                    continue
                if session_id == recorded_session_id:
                    continue
                if not display or display == "/tui fullscreen":
                    return None
                successor_timestamp = _extract_history_timestamp(payload)
                if (
                    trampoline_timestamp is not None
                    and successor_timestamp is not None
                    and successor_timestamp - trampoline_timestamp > 120
                ):
                    return candidate_session_id
                if session_id in prior_same_project_session_ids:
                    return None
                if not _valid_successor_transcript(
                    project_dir=project_dir,
                    session_id=session_id,
                    history_display=display,
                    history_timestamp=successor_timestamp,
                    started_at_epoch=started_at_epoch,
                ):
                    return None
                if candidate_session_id is None:
                    candidate_session_id = session_id
                    continue
                if session_id != candidate_session_id:
                    return None
    except OSError:
        logger.debug("Failed to read Claude history file %s", history_path, exc_info=True)
    return candidate_session_id


def reconcile_tui_trampoline_session_id(
    *,
    project_root: Path,
    recorded_session_id: str,
    started_at_epoch: float | None = None,
) -> str | None:
    """Return a durable Claude transcript ID when a recorded ID is a TUI trampoline.

    The recorded ID is preserved if its transcript exists.  A replacement is accepted
    only when Claude prompt history shows the recorded ID entered `/tui fullscreen`
    for the same project and the next same-project prompt matches the first user
    prompt in a different session's durable transcript file.
    """

    normalized_session_id = recorded_session_id.strip()
    if not normalized_session_id:
        return None
    transcript_path = _claude_project_dir(project_root) / f"{normalized_session_id}.jsonl"
    if transcript_path.is_file():
        return normalized_session_id
    return _find_tui_trampoline_successor_session_id(
        project_root=project_root,
        recorded_session_id=normalized_session_id,
        started_at_epoch=started_at_epoch,
    )


def detect_primary_session_id(
    project_root: Path,
    started_at_epoch: float,
    *,
    expected_session_id: str | None = None,
) -> str | None:
    """Detect Claude primary session ID by verifying a known session file only."""
    if not expected_session_id:
        logger.debug("No expected session ID for primary detection; skipping heuristic scan")
        return None

    project_dir = _claude_project_dir(project_root)
    if not project_dir.is_dir():
        logger.warning(
            "Expected Claude session directory not found",
            extra={"session_id": expected_session_id, "project_dir": str(project_dir)},
        )
        return None

    candidate = project_dir / f"{expected_session_id}.jsonl"
    try:
        if not candidate.is_file():
            logger.warning(
                "Expected Claude session file not found",
                extra={"session_id": expected_session_id, "project_dir": str(project_dir)},
            )
            return None
        if candidate.stat().st_mtime + 1 < started_at_epoch:
            return None
        resolved = _read_claude_session_id(candidate)
        if resolved == expected_session_id:
            return expected_session_id
        logger.warning(
            "Claude session file exists but embedded ID mismatches",
            extra={"expected": expected_session_id, "found": resolved},
        )
    except OSError:
        logger.debug("Failed to verify Claude session file", exc_info=True)
    return None

