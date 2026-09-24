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
    read_pi_exact_content,
)
from meridian.lib.harness.pi_paths import resolve_pi_agent_dir, resolve_pi_spawn_session_root
from meridian.lib.state.session_authority import LocalObjectStamp, RecordedNativeSource


@dataclass(frozen=True)
class NativeModelReadContext:
    """Source-session paths a reader needs to locate the native store."""

    project_root: str | None = None
    claude_config_dir: str | None = None
    pi_session_dir: str | None = None
    launch_env: Mapping[str, str] | None = None


@dataclass(frozen=True)
class ExactModelObservation:
    source: RecordedNativeSource
    provider_contract: str
    view_basis: Literal["reopen-default"]
    selected_leaf_id: str | None
    model_basis: Literal["selected_reopen_default"]
    model_token: str
    native_provider: str
    evidence_entry_id: str
    byte_length: int
    content_sha256: str
    file_object: LocalObjectStamp
    store_object: LocalObjectStamp
    observed_at: str
    complete: Literal[True] = True


@dataclass(frozen=True)
class ModelEvidenceUnavailable:
    reason: Literal[
        "unsupported_provider",
        "unsupported_view",
        "missing",
        "inaccessible",
        "unsupported_dialect",
        "incomplete",
        "no_model",
        "changed_during_read",
    ]


@dataclass(frozen=True)
class ModelSourceConflict:
    reason: Literal["source_mismatch", "store_changed", "file_changed", "identity_mismatch"]


type ExactModelEvidence = ExactModelObservation | ModelEvidenceUnavailable | ModelSourceConflict


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
    content = read_pi_exact_content(source)
    if isinstance(content, PiExactContentConflict):
        return ModelSourceConflict(content.reason)
    if isinstance(content, PiExactContentUnavailable):
        return ModelEvidenceUnavailable(content.reason)
    assert isinstance(content, PiExactContent)
    try:
        text = content.data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return ModelEvidenceUnavailable("incomplete")
    projection = project_pi_reopen_default(text)
    if not projection.complete:
        if any("unsupported Pi native dialect" in reason for reason in projection.reasons):
            return ModelEvidenceUnavailable("unsupported_dialect")
        return ModelEvidenceUnavailable("incomplete")
    header = projection.events[0]
    if header.get("id") != source.key.native_session_id:
        return ModelSourceConflict("identity_mismatch")
    selected: tuple[str, str, str] | None = None
    leaf_id: str | None = None
    for row in projection.events[1:]:
        entry_id = row.get("id")
        if isinstance(entry_id, str):
            leaf_id = entry_id
        entry_type = row.get("type")
        if entry_type == "model_change":
            provider, model = row.get("provider"), row.get("modelId")
            if (
                not isinstance(provider, str)
                or not provider.strip()
                or not isinstance(model, str)
                or not model.strip()
            ):
                return ModelEvidenceUnavailable("incomplete")
            selected = (provider, model, str(entry_id))
        elif entry_type == "message":
            message = row.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("role"), str):
                return ModelEvidenceUnavailable("incomplete")
            if message.get("role") == "assistant":
                provider, model = message.get("provider"), message.get("model")
                if (
                    not isinstance(provider, str)
                    or not provider.strip()
                    or not isinstance(model, str)
                    or not model.strip()
                ):
                    return ModelEvidenceUnavailable("incomplete")
                selected = (provider, model, str(entry_id))
    if selected is None:
        return ModelEvidenceUnavailable("no_model")
    provider, model, entry_id = selected
    return ExactModelObservation(
        source=source,
        provider_contract="pi-0.87.1-legacy-v3-settings-v1",
        view_basis="reopen-default",
        selected_leaf_id=leaf_id,
        model_basis="selected_reopen_default",
        model_token=model,
        native_provider=provider,
        evidence_entry_id=entry_id,
        byte_length=content.byte_length,
        content_sha256=content.content_sha256,
        file_object=content.file_object,
        store_object=content.store_object,
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
