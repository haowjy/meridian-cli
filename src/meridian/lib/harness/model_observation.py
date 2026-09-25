"""Native-history readers, including exact selected reopen-setting evidence.

The exact Pi evidence reports a selected reopen default, not a model that
actually executed or a live process cursor. Legacy discovery readers remain
best-effort and return a routable model token or ``None``.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from meridian.lib.harness import claude_sessions, codex_rollout, opencode_transcript
from meridian.lib.harness.codex_rollout import CODEX_ROLLOUT_FILENAME_RE
from meridian.lib.harness.pi_journal import project_pi_reopen_default
from meridian.lib.harness.pi_native_source import (
    PiExactContent,
    PiExactContentConflict,
    PiExactContentUnavailable,
    PiExactValidatedContent,
    read_pi_exact_content,
)
from meridian.lib.harness.pi_paths import resolve_pi_agent_dir, resolve_pi_spawn_session_root
from meridian.lib.state.session_authority import (
    ExactModelEvidence,
    ExactModelObservation,
    ModelEvidenceUnavailable,
    ModelSourceConflict,
    RecordedNativeSource,
)


@dataclass(frozen=True)
class NativeModelReadContext:
    """Source-session paths a reader needs to locate the native store."""

    project_root: str | None = None
    claude_config_dir: str | None = None
    pi_session_dir: str | None = None
    launch_env: Mapping[str, str] | None = None


@dataclass(frozen=True)
class _PiSelection:
    leaf_id: str | None
    provider: str
    model: str
    entry_id: str


def _validate_pi_horizon(
    content: PiExactContent, source: RecordedNativeSource
) -> _PiSelection | ModelEvidenceUnavailable | ModelSourceConflict:
    data = content.data
    # Header identity is an independent fact: check it before tail decoding or
    # whole-journal completeness can downgrade a known conflict.
    first_line = next((line for line in data.splitlines() if line.strip()), b"")
    try:
        header_payload: object = json.loads(first_line.decode("utf-8", errors="strict"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        header_payload = None
    if (
        isinstance(header_payload, dict)
        and cast("dict[str, object]", header_payload).get("type") == "session"
        and cast("dict[str, object]", header_payload).get("id")
        != source.key.native_session_id
    ):
        return ModelSourceConflict("identity_mismatch")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return ModelEvidenceUnavailable("incomplete")
    projection = project_pi_reopen_default(text)
    if projection.events and projection.events[0].get("id") != source.key.native_session_id:
        return ModelSourceConflict("identity_mismatch")
    if not projection.complete:
        if "unsupported_dialect" in projection.reasons:
            return ModelEvidenceUnavailable("unsupported_dialect")
        return ModelEvidenceUnavailable("incomplete")
    selected: tuple[str, str, str] | None = None
    leaf_id: str | None = None
    for row in projection.events[1:]:
        entry_id = row.get("id")
        if isinstance(entry_id, str):
            leaf_id = entry_id
        entry_type = row.get("type")
        if entry_type == "model_change":
            selected = (cast("str", row["provider"]), cast("str", row["modelId"]), str(entry_id))
        elif entry_type == "message":
            message = cast("dict[str, object]", row["message"])
            if message.get("role") == "assistant":
                selected = (
                    cast("str", message["provider"]),
                    cast("str", message["model"]),
                    str(entry_id),
                )
    if selected is None:
        return ModelEvidenceUnavailable("no_model")
    return _PiSelection(leaf_id, *selected)


def read_model_evidence_exact(
    source: RecordedNativeSource,
    *,
    view: Literal["reopen-default", "process-active"] = "reopen-default",
) -> ExactModelEvidence:
    """Read selected Pi reopen settings from one exact, recorded native file."""
    if view != "reopen-default":
        return ModelEvidenceUnavailable("unsupported_view")
    if source.key.harness != "pi":
        return ModelEvidenceUnavailable("unsupported_provider")
    content = read_pi_exact_content(
        source, validate=lambda value: _validate_pi_horizon(value, source)
    )
    if isinstance(content, PiExactContentConflict):
        return ModelSourceConflict(content.reason)
    if isinstance(content, PiExactContentUnavailable):
        return ModelEvidenceUnavailable(content.reason)
    assert isinstance(content, PiExactValidatedContent)
    if isinstance(content.value, (ModelEvidenceUnavailable, ModelSourceConflict)):
        return content.value
    selection = content.value
    evidence = content.content
    return ExactModelObservation(
        source=source,
        provider_contract="pi-0.87.1-legacy-v3-settings-v1",
        view_basis="reopen-default",
        selected_leaf_id=selection.leaf_id,
        model_basis="selected_reopen_default",
        model_token=selection.model,
        native_provider=selection.provider,
        evidence_entry_id=selection.entry_id,
        byte_length=evidence.byte_length,
        content_sha256=evidence.content_sha256,
        file_object=evidence.file_object,
        store_object=evidence.store_object,
        observed_at=datetime.now(UTC).isoformat(),
    )


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


def _read_claude_last_model(
    project_root: Path,
    config_root_hint: Path | None,
    session_id: str,
) -> str | None:
    for project_dir in claude_sessions.candidate_claude_project_dirs(
        project_root, config_root_hint
    ):
        last: str | None = None
        for payload in _iter_json_objects(project_dir / f"{session_id}.jsonl"):
            if payload.get("type") == "assistant":
                last = _nested_str(payload, "message", "model") or last
        if last is not None:
            return last
    return None


def _read_codex_last_model(codex_home: Path, session_id: str) -> str | None:
    sessions_root = codex_home / "sessions"
    if not sessions_root.is_dir():
        return None
    candidate = next(
        (
            path
            for path in sessions_root.rglob(f"rollout-*-{session_id}.jsonl")
            if CODEX_ROLLOUT_FILENAME_RE.match(path.name) is not None
        ),
        None,
    )
    if candidate is None:
        return None
    last_turn_model: str | None = None
    fallback_model: str | None = None
    for payload in _iter_json_objects(candidate):
        event_type = payload.get("type")
        if event_type == "turn_context":
            last_turn_model = _nested_str(payload, "payload", "model") or last_turn_model
        elif event_type == "world_state":
            fallback_model = _nested_str(payload, "payload", "state", "model") or fallback_model
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
    candidate = next(
        (path for path in session_root.rglob("*.jsonl") if session_id in path.name),
        None,
    )
    if candidate is None:
        return None

    last_model_id: str | None = None
    session_model_id: str | None = None
    for payload in _iter_json_objects(candidate):
        event_type = payload.get("type")
        if event_type == "model_change":
            last_model_id = _nested_str(payload, "modelId") or last_model_id
        elif event_type == "session":
            session_model_id = _nested_str(payload, "model") or _nested_str(
                payload, "model", "modelId"
            )
    return last_model_id or session_model_id


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
                Path(context.claude_config_dir).expanduser() if context.claude_config_dir else None
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
