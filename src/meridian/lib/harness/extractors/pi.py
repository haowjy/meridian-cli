"""Pi harness extractor."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from meridian.lib.core.domain import TokenUsage
from meridian.lib.core.types import ArtifactKey, SpawnId
from meridian.lib.harness.adapter import ArtifactStore
from meridian.lib.harness.common import (
    OUTPUT_FILENAME,
    _coerce_optional_int,  # pyright: ignore[reportPrivateUsage]
    _iter_json_lines_artifact,  # pyright: ignore[reportPrivateUsage]
)
from meridian.lib.harness.connections.base import HarnessEvent
from meridian.lib.harness.pi_paths import resolve_pi_spawn_session_root
from meridian.lib.launch.constants import HISTORY_FILENAME
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.launch.report import extract_pi_failure_from_history
from meridian.lib.platform import IS_WINDOWS

from .base import HarnessExtractor

PiSessionDiscovery = Literal["ok", "never_created", "discovery_failed"]


@dataclass(frozen=True)
class PiSessionDiscoveryResult:
    """Outcome of one primary Pi session-id discovery attempt."""

    session_id: str | None
    discovery: PiSessionDiscovery
    detail: str | None = None



def _pi_session_root(launch_env: Mapping[str, str]) -> Path:
    session_dir_override = launch_env.get("PI_CODING_AGENT_SESSION_DIR", "").strip()
    if session_dir_override:
        return Path(session_dir_override).expanduser()

    agent_dir = launch_env.get("PI_CODING_AGENT_DIR", "").strip()
    if agent_dir:
        return Path(agent_dir).expanduser() / "sessions"

    return resolve_pi_spawn_session_root(env=launch_env)


def _iter_session_id_candidates_from_artifacts(
    artifacts: ArtifactStore,
    spawn_id: SpawnId,
) -> list[str]:
    payloads = _iter_json_lines_artifact(artifacts, spawn_id, OUTPUT_FILENAME)
    session_ids: list[str] = []
    for payload in payloads:
        if str(payload.get("type", "")).strip().lower() != "session":
            continue
        session_id = payload.get("id")
        if isinstance(session_id, str) and session_id.strip():
            session_ids.append(session_id.strip())
    return session_ids


def _normalize_cwd_for_matching(value: Path | str) -> str | None:
    try:
        raw_path = value if isinstance(value, Path) else Path(value)
    except (TypeError, ValueError):
        return None
    try:
        resolved_path = raw_path.expanduser().resolve()
    except OSError:
        resolved_path = raw_path.expanduser().absolute()
    normalized = str(resolved_path).replace("\\", "/").rstrip("/")
    return normalized or "/"


def _cwd_match_key(value: str) -> str:
    """Return the comparison key for a resolved session cwd.

    Pi session files store the cwd as text. On Windows, the filesystem path
    identity Meridian is trying to recover is case-insensitive, so compare the
    normalized text with Windows case-folding rather than exact string equality.
    """

    return value.casefold() if IS_WINDOWS else value


def _session_discovery_result_from_session_files(
    *,
    launch_env: Mapping[str, str],
    child_cwd: Path,
    started_at_epoch: float | None = None,
    expected_session_id: str | None = None,
) -> PiSessionDiscoveryResult:
    root = _pi_session_root(launch_env).expanduser()
    if not root.is_dir():
        return PiSessionDiscoveryResult(
            session_id=None,
            discovery="never_created",
            detail=f"session_dir_missing: {root}",
        )

    session_files = list(root.glob("*.jsonl"))
    if not session_files:
        return PiSessionDiscoveryResult(
            session_id=None,
            discovery="never_created",
            detail=f"no_session_files_in_dir: {root}",
        )

    candidates: list[tuple[float, str]] = []
    parse_error_detail: str | None = None
    normalized_child_cwd = _normalize_cwd_for_matching(child_cwd)
    if normalized_child_cwd is None:
        no_match_after = started_at_epoch - 2.0 if started_at_epoch is not None else "any"
        return PiSessionDiscoveryResult(
            session_id=None,
            discovery="discovery_failed",
            detail=f"no_matching_session: cwd={child_cwd} after={no_match_after}",
        )

    recent_threshold = started_at_epoch - 2.0 if started_at_epoch is not None else None
    for session_file in session_files:
        try:
            modified_at = session_file.stat().st_mtime
        except OSError:
            continue
        if recent_threshold is not None and modified_at < recent_threshold:
            continue
        try:
            with session_file.open(encoding="utf-8", errors="ignore") as handle:
                first_line = handle.readline()
        except OSError as exc:
            if parse_error_detail is None:
                parse_error_detail = f"{session_file.name}: {exc}"
            continue
        if not first_line.strip():
            continue
        try:
            payload_obj = json.loads(first_line)
        except json.JSONDecodeError as exc:
            if parse_error_detail is None:
                parse_error_detail = f"{session_file.name}: {exc.msg}"
            continue
        if not isinstance(payload_obj, dict):
            continue
        payload = cast("dict[str, object]", payload_obj)
        if str(payload.get("type", "")).strip().lower() != "session":
            continue
        session_id = payload.get("id")
        if not isinstance(session_id, str) or not session_id.strip():
            continue

        session_cwd = payload.get("cwd")
        if not isinstance(session_cwd, str):
            continue
        normalized_session_cwd = _normalize_cwd_for_matching(session_cwd)
        if normalized_session_cwd is None or _cwd_match_key(
            normalized_session_cwd
        ) != _cwd_match_key(normalized_child_cwd):
            continue

        candidates.append((modified_at, session_id.strip()))

    if not candidates:
        if parse_error_detail is not None:
            return PiSessionDiscoveryResult(
                session_id=None,
                discovery="discovery_failed",
                detail=f"session_file_parse_error: {parse_error_detail}",
            )
        return PiSessionDiscoveryResult(
            session_id=None,
            discovery="discovery_failed",
            detail=(
                f"no_matching_session: cwd={normalized_child_cwd} "
                f"after={recent_threshold if recent_threshold is not None else 'any'}"
            ),
        )

    sorted_candidates = sorted(candidates, key=lambda item: item[0], reverse=True)
    normalized_expected_session_id = (expected_session_id or "").strip()
    filtered_candidates = (
        [item for item in sorted_candidates if item[1] != normalized_expected_session_id]
        if normalized_expected_session_id
        else sorted_candidates
    )
    if not filtered_candidates:
        return PiSessionDiscoveryResult(
            session_id=None,
            discovery="discovery_failed",
            detail=(
                f"no_matching_session: cwd={normalized_child_cwd} "
                f"after={recent_threshold if recent_threshold is not None else 'any'}"
            ),
        )

    return PiSessionDiscoveryResult(
        session_id=filtered_candidates[0][1],
        discovery="ok",
        detail=None,
    )


def _session_id_from_session_files(
    *,
    launch_env: Mapping[str, str],
    child_cwd: Path,
    started_at_epoch: float | None = None,
    expected_session_id: str | None = None,
) -> str | None:
    return _session_discovery_result_from_session_files(
        launch_env=launch_env,
        child_cwd=child_cwd,
        started_at_epoch=started_at_epoch,
        expected_session_id=expected_session_id,
    ).session_id


def detect_pi_session_id_from_session_files(
    *,
    launch_env: Mapping[str, str],
    child_cwd: Path,
    started_at_epoch: float | None = None,
    expected_session_id: str | None = None,
) -> str | None:
    """Detect Pi session id from persisted session files for one cwd bucket."""

    return _session_id_from_session_files(
        launch_env=launch_env,
        child_cwd=child_cwd,
        started_at_epoch=started_at_epoch,
        expected_session_id=expected_session_id,
    )


def detect_pi_session_discovery_from_session_files(
    *,
    launch_env: Mapping[str, str],
    child_cwd: Path,
    started_at_epoch: float | None = None,
    expected_session_id: str | None = None,
) -> PiSessionDiscoveryResult:
    """Detect Pi session id + discovery status from persisted flat session files."""

    return _session_discovery_result_from_session_files(
        launch_env=launch_env,
        child_cwd=child_cwd,
        started_at_epoch=started_at_epoch,
        expected_session_id=expected_session_id,
    )


def _extract_usage_from_message_end(payloads: list[dict[str, object]]) -> TokenUsage:
    for payload in reversed(payloads):
        if str(payload.get("type", "")).strip().lower() != "message_end":
            continue
        message_obj = payload.get("message")
        if not isinstance(message_obj, dict):
            continue
        message = cast("dict[str, object]", message_obj)
        if str(message.get("role", "")).strip().lower() != "assistant":
            continue
        usage_obj = message.get("usage")
        if not isinstance(usage_obj, dict):
            continue
        usage = cast("dict[str, object]", usage_obj)
        return TokenUsage(
            input_tokens=_coerce_optional_int(usage.get("input")),
            output_tokens=_coerce_optional_int(usage.get("output")),
            cache_read_input_tokens=_coerce_optional_int(usage.get("cacheRead")),
            cache_creation_input_tokens=_coerce_optional_int(usage.get("cacheWrite")),
            total_cost_usd=(
                float(total)
                if isinstance((cost := usage.get("cost")), dict)
                and (total := cast("dict[str, object]", cost).get("total")) is not None
                and isinstance(total, int | float)
                else None
            ),
        )
    return TokenUsage()


def _assistant_message_text(message: Mapping[str, object]) -> str | None:
    if str(message.get("role", "")).strip().lower() != "assistant":
        return None

    content_obj = message.get("content")
    if not isinstance(content_obj, list):
        return None

    texts: list[str] = []
    for part_obj in cast("list[object]", content_obj):
        if not isinstance(part_obj, dict):
            continue
        part = cast("dict[str, object]", part_obj)
        if str(part.get("type", "")).strip().lower() != "text":
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            texts.append(text)

    if not texts:
        return None
    return "\n".join(texts)


def _read_artifact_text(artifacts: ArtifactStore, spawn_id: SpawnId, name: str) -> str:
    key = ArtifactKey(f"{spawn_id}/{name}")
    if not artifacts.exists(key):
        return ""
    return artifacts.get(key).decode("utf-8", errors="ignore")


class PiHarnessExtractor(HarnessExtractor[ResolvedLaunchSpec]):
    """Extractor implementation for Pi artifacts and events."""

    def detect_session_id_from_event(self, event: HarnessEvent) -> str | None:
        if event.event_type != "session":
            return None
        session_id = event.payload.get("id")
        if isinstance(session_id, str) and session_id.strip():
            return session_id.strip()
        return None

    def detect_session_id_from_artifacts(
        self,
        *,
        spec: ResolvedLaunchSpec,
        launch_env: Mapping[str, str],
        child_cwd: Path,
        runtime_root: Path,
    ) -> str | None:
        _ = runtime_root
        if (
            spec.continue_session_id
            and spec.continue_session_id.strip()
            and not spec.continue_fork
        ):
            return spec.continue_session_id.strip()

        return detect_pi_session_id_from_session_files(
            launch_env=launch_env,
            child_cwd=child_cwd,
        )

    def extract_usage(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> TokenUsage:
        payloads = _iter_json_lines_artifact(artifacts, spawn_id, OUTPUT_FILENAME)
        return _extract_usage_from_message_end(payloads)

    def extract_session_id(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        candidates = _iter_session_id_candidates_from_artifacts(artifacts, spawn_id)
        if not candidates:
            return None
        return candidates[0]

    def extract_report(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        history_text = _read_artifact_text(artifacts, spawn_id, HISTORY_FILENAME)
        if history_text.strip():
            pi_failure = extract_pi_failure_from_history(history_text)
            if pi_failure:
                return pi_failure

        payloads = _iter_json_lines_artifact(artifacts, spawn_id, OUTPUT_FILENAME)
        for payload in reversed(payloads):
            event_type = str(payload.get("type", "")).strip().lower()
            if event_type == "response":
                command = str(payload.get("command", "")).strip().lower()
                if command == "prompt" and payload.get("success") is False:
                    error = payload.get("error")
                    if isinstance(error, str) and error.strip():
                        return error.strip()
                    return "pi_prompt_rejected"
            if event_type != "agent_end":
                continue
            messages_obj = payload.get("messages")
            if not isinstance(messages_obj, list):
                continue
            for message_obj in reversed(cast("list[object]", messages_obj)):
                if not isinstance(message_obj, dict):
                    continue
                text = _assistant_message_text(cast("dict[str, object]", message_obj))
                if text:
                    return text
        return None


PI_EXTRACTOR = PiHarnessExtractor()

__all__ = [
    "PI_EXTRACTOR",
    "PiHarnessExtractor",
    "detect_pi_session_discovery_from_session_files",
    "detect_pi_session_id_from_session_files",
]
