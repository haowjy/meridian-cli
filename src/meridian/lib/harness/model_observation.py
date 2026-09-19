"""Native-history read seam for the last executed conversation model.

Harness-specific session stores are read here to recover the model a harness
actually last executed — as opposed to the model Meridian selected at startup.
Every reader returns a routable model token (verbatim, never remapped) or ``None``;
unknown harnesses, missing stores, unparseable data, and I/O errors never raise.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from meridian.lib.harness import claude_sessions, codex_rollout, opencode_transcript
from meridian.lib.harness.codex_rollout import CODEX_ROLLOUT_FILENAME_RE
from meridian.lib.harness.pi_paths import resolve_pi_agent_dir, resolve_pi_spawn_session_root


@dataclass(frozen=True)
class NativeModelReadContext:
    """Source-session paths a reader needs to locate the native store."""

    project_root: str | None = None
    claude_config_dir: str | None = None
    pi_session_dir: str | None = None
    launch_env: Mapping[str, str] | None = None


def _iter_json_objects(path: Path) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
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
                    payloads.append(cast("dict[str, object]", payload))
    except OSError:
        return []
    return payloads


def _read_claude_last_model(
    project_root: Path, config_root_hint: Path | None, session_id: str,
) -> str | None:
    for project_dir in claude_sessions.candidate_claude_project_dirs(
        project_root, config_root_hint
    ):
        transcript_path = project_dir / f"{session_id}.jsonl"
        last: str | None = None
        for payload in _iter_json_objects(transcript_path):
            if payload.get("type") != "assistant":
                continue
            message = payload.get("message")
            if not isinstance(message, dict):
                continue
            model = cast("dict[str, object]", message).get("model")
            if isinstance(model, str) and model.strip():
                last = model.strip()
        if last is not None:
            return last
    return None


def _read_codex_last_model(codex_home: Path, session_id: str) -> str | None:
    sessions_root = codex_home / "sessions"
    if not sessions_root.is_dir():
        return None
    candidate: Path | None = None
    for path in sessions_root.rglob(f"rollout-*-{session_id}.jsonl"):
        if CODEX_ROLLOUT_FILENAME_RE.match(path.name) is not None:
            candidate = path
            break
    if candidate is None:
        return None
    last_turn_model: str | None = None
    fallback_model: str | None = None
    for payload in _iter_json_objects(candidate):
        event_type = payload.get("type")
        raw = payload.get("payload")
        if not isinstance(raw, dict):
            continue
        raw_payload = cast("dict[str, object]", raw)
        if event_type == "turn_context":
            model = raw_payload.get("model")
            if isinstance(model, str) and model.strip():
                last_turn_model = model.strip()
        elif event_type == "world_state":
            state = raw_payload.get("state")
            if isinstance(state, dict):
                model = cast("dict[str, object]", state).get("model")
                if isinstance(model, str) and model.strip():
                    fallback_model = model.strip()
    return last_turn_model or fallback_model


def _pi_session_roots(context: NativeModelReadContext) -> tuple[Path, ...]:
    """Candidate Pi session roots, most specific first.

    Interactive Pi sessions live under the agent dir (``~/.pi/agent/sessions``),
    while Meridian-managed spawns use the spawn session root. A session id may
    resolve from either, so both are searched.
    """

    env = context.launch_env if context.launch_env is not None else os.environ
    roots: list[Path] = []

    def _add(candidate: Path) -> None:
        resolved = candidate.expanduser()
        if resolved not in roots:
            roots.append(resolved)

    if context.pi_session_dir:
        _add(Path(context.pi_session_dir))
    session_dir_override = env.get("PI_CODING_AGENT_SESSION_DIR", "").strip()
    if session_dir_override:
        _add(Path(session_dir_override))
    agent_dir = env.get("PI_CODING_AGENT_DIR", "").strip()
    if agent_dir:
        _add(Path(agent_dir) / "sessions")
    else:
        _add(resolve_pi_agent_dir(env=env) / "sessions")
    _add(resolve_pi_spawn_session_root(env=env))
    return tuple(roots)


def _read_pi_last_model(session_root: Path, session_id: str) -> str | None:
    if not session_root.is_dir():
        return None
    candidate: Path | None = None
    for path in session_root.rglob("*.jsonl"):
        if session_id in path.name:
            candidate = path
            break
    if candidate is None:
        return None

    last_model_id: str | None = None
    session_model_id: str | None = None
    for payload in _iter_json_objects(candidate):
        event_type = payload.get("type")
        if event_type == "model_change":
            model_id = payload.get("modelId")
            if isinstance(model_id, str) and model_id.strip():
                last_model_id = model_id.strip()
        elif event_type == "session":
            session_model_id = _extract_pi_model_id(payload)
    return last_model_id or session_model_id


def _extract_pi_model_id(payload: dict[str, object]) -> str | None:
    raw_model = payload.get("model")
    if isinstance(raw_model, str) and raw_model.strip():
        return raw_model.strip()
    if isinstance(raw_model, dict):
        model_id = cast("dict[str, object]", raw_model).get("modelId")
        if isinstance(model_id, str) and model_id.strip():
            return model_id.strip()
    return None


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
    if not normalized_session_id:
        return None
    try:
        if harness == "opencode":
            return opencode_transcript.read_last_model(
                normalized_session_id, launch_env=context.launch_env
            )
        if harness == "claude":
            if context.project_root is None:
                return None
            config_root_hint = (
                Path(context.claude_config_dir).expanduser()
                if context.claude_config_dir
                else None
            )
            return _read_claude_last_model(
                Path(context.project_root).expanduser(),
                config_root_hint,
                normalized_session_id,
            )
        if harness == "codex":
            env = context.launch_env if context.launch_env is not None else os.environ
            return _read_codex_last_model(
                codex_rollout.resolve_codex_home(env), normalized_session_id
            )
        if harness == "pi":
            for session_root in _pi_session_roots(context):
                token = _read_pi_last_model(session_root, normalized_session_id)
                if token is not None:
                    return token
            return None
    except (OSError, RuntimeError, sqlite3.Error):
        return None
    return None


__all__ = [
    "NativeModelReadContext",
    "read_last_executed_model",
]
