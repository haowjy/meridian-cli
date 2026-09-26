"""Claude on-disk session and history helpers."""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import cast

from meridian.lib.platform import get_home_path

logger = logging.getLogger(__name__)


def project_slug(project_root: Path) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", str(project_root.resolve()))


def resolve_claude_config_root(env: Mapping[str, str], cwd: Path) -> Path:
    home = Path(env["HOME"]) if env.get("HOME") else get_home_path()
    configured = env.get("CLAUDE_CONFIG_DIR", "").strip()
    root = Path(configured) if configured else home / ".claude"
    if configured == "~" or configured.startswith("~/"):
        root = home / configured.removeprefix("~").lstrip("/")
    if not root.is_absolute():
        root = cwd / root
    return root.resolve()


def _claude_config_root() -> Path:
    return resolve_claude_config_root(os.environ, Path.cwd())


def _claude_projects_root() -> Path:
    return _claude_config_root() / "projects"


def _claude_history_path() -> Path:
    return _claude_config_root() / "history.jsonl"


def _claude_project_dir(project_root: Path) -> Path:
    return _claude_projects_root() / project_slug(project_root)


def candidate_claude_project_dirs(
    project_root: Path,
    config_root_hint: Path | None = None,
) -> list[Path]:
    """Return Claude project directories ordered by transcript lookup trust."""
    slug = project_slug(project_root)
    if config_root_hint is None:
        return [_claude_config_root() / "projects" / slug]
    return [config_root_hint / "projects" / slug]


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
    native_store: Path | None = None,
) -> str | None:
    history_path = (
        native_store.parent.parent / "history.jsonl"
        if native_store is not None
        else _claude_history_path()
    )
    project_dir = native_store or _claude_project_dir(project_root)
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
    native_store: Path | None = None,
) -> str | None:
    """Diagnose a TUI trampoline successor without changing the recorded identity."""

    normalized_session_id = recorded_session_id.strip()
    if not normalized_session_id:
        return None
    project_dir = native_store or _claude_project_dir(project_root)
    transcript_path = project_dir / f"{normalized_session_id}.jsonl"
    if transcript_path.is_file():
        return normalized_session_id
    trampoline_successor_id = _find_tui_trampoline_successor_session_id(
        project_root=project_root,
        recorded_session_id=normalized_session_id,
        started_at_epoch=started_at_epoch,
        native_store=native_store,
    )
    if trampoline_successor_id and trampoline_successor_id != normalized_session_id:
        logger.warning(
            "claude_trampoline_successor",
            extra={
                "kept": normalized_session_id,
                "attempted": trampoline_successor_id,
                "source": "trampoline",
            },
        )
        return trampoline_successor_id
    return normalized_session_id
