"""OpenCode harness extractor."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from meridian.lib.core.domain import TokenUsage
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.adapter import ArtifactStore
from meridian.lib.harness.common import (
    OUTPUT_FILENAME,
    _coerce_optional_int,
    _iter_json_lines_artifact,
    coerce_optional_float,
    extract_opencode_report,
    extract_session_id_from_artifacts_with_patterns,
    extract_usage_from_artifacts,
)
from meridian.lib.harness.connections.base import HarnessEvent
from meridian.lib.harness.launch_spec import OpenCodeLaunchSpec
from meridian.lib.harness.opencode_storage import (
    iter_opencode_session_files,
    opencode_session_id_from_path,
    resolve_opencode_storage_root,
)
from meridian.lib.platform import get_home_path

from .base import HarnessExtractor, session_from_mapping_with_keys

_SESSION_ID_TEXT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\bopencode\b[^\n]*?--session(?:=|\s+)([A-Za-z0-9][A-Za-z0-9._:-]{5,})\b",
        re.IGNORECASE,
    ),
)
_OPENCODE_SESSION_CREATED_RE = re.compile(
    r"^\w+\s+(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\s+\+\d+ms\s+"
    r"service=session\s+id=(?P<session_id>\S+)\s+.*?\bdirectory=(?P<directory>\S+)\b.*\bcreated\b"
)
_SESSION_MATCH_WINDOW_SECONDS = 15 * 60
_PATH_HINT_KEYS: frozenset[str] = frozenset(
    {
        "directory",
        "cwd",
        "project_root",
        "repoRoot",
        "state_root",
        "stateRoot",
        "project_dir",
        "projectDir",
    }
)


def _resolve_logs_root(launch_env: Mapping[str, str]) -> Path:
    explicit = launch_env.get("OPENCODE_LOG_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()

    opencode_home = launch_env.get("OPENCODE_HOME", "").strip()
    if opencode_home:
        return Path(opencode_home).expanduser() / "log"

    xdg_data_home = launch_env.get("XDG_DATA_HOME", "").strip()
    if xdg_data_home:
        return Path(xdg_data_home).expanduser() / "opencode" / "log"

    home = launch_env.get("HOME", "").strip()
    if home:
        return Path(home).expanduser() / ".local" / "share" / "opencode" / "log"

    return get_home_path() / ".local" / "share" / "opencode" / "log"


def _safe_resolve(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except OSError:
        return path.expanduser().absolute()


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _matches_spawn_paths(value: str, targets: tuple[Path, ...]) -> bool:
    normalized = value.strip()
    if not normalized:
        return False
    try:
        candidate = _safe_resolve(Path(normalized))
    except (TypeError, ValueError, OSError):
        return False
    return any(_paths_overlap(candidate, target) for target in targets)


def _payload_matches_spawn(payload: object, targets: tuple[Path, ...]) -> bool:
    if isinstance(payload, dict):
        mapping = cast("dict[str, object]", payload)
        for key, value in mapping.items():
            if (
                isinstance(value, str)
                and key in _PATH_HINT_KEYS
                and _matches_spawn_paths(value, targets)
            ):
                return True
            if _payload_matches_spawn(value, targets):
                return True
        return False

    if isinstance(payload, list):
        items = cast("list[object]", payload)
        return any(_payload_matches_spawn(item, targets) for item in items)

    return False


def _parse_start_time(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _latest_spawn_start_epoch(*, runtime_root: Path, child_cwd: Path) -> float | None:
    from meridian.lib.state import spawn_store

    resolved_child = _safe_resolve(child_cwd)
    latest_any: float | None = None
    latest_matching_child: float | None = None

    try:
        spawns = spawn_store.list_spawns(runtime_root)
    except OSError:
        return None

    for spawn in spawns:
        if (spawn.harness or "").strip().lower() != "opencode":
            continue

        started_at = _parse_start_time(spawn.started_at)
        if started_at is None:
            continue
        if latest_any is None or started_at > latest_any:
            latest_any = started_at

        if spawn.execution_cwd is None or not spawn.execution_cwd.strip():
            continue
        try:
            resolved_execution_cwd = _safe_resolve(Path(spawn.execution_cwd))
        except OSError:
            continue
        if resolved_execution_cwd != resolved_child:
            continue
        if latest_matching_child is None or started_at > latest_matching_child:
            latest_matching_child = started_at

    return latest_matching_child if latest_matching_child is not None else latest_any


def _detect_primary_session_id(
    *,
    child_cwd: Path,
    launch_env: Mapping[str, str],
) -> str | None:
    logs_root = _resolve_logs_root(launch_env)
    if not logs_root.is_dir():
        return None

    resolved_repo = child_cwd.resolve()
    matches: list[tuple[str, str]] = []
    for candidate in logs_root.glob("*.log"):
        try:
            lines = candidate.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in lines:
            match = _OPENCODE_SESSION_CREATED_RE.match(line)
            if match is None:
                continue
            directory = match.group("directory")
            try:
                directory_matches = Path(directory).expanduser().resolve() == resolved_repo
            except OSError:
                continue
            if not directory_matches:
                continue
            session_id = match.group("session_id").strip()
            if session_id:
                matches.append((match.group("ts"), session_id))

    if not matches:
        return None

    matches.sort(key=lambda item: item[0], reverse=True)
    return matches[0][1]


def _detect_storage_session_id(
    *,
    launch_env: Mapping[str, str],
    child_cwd: Path,
    runtime_root: Path,
) -> str | None:
    storage_root = resolve_opencode_storage_root(launch_env)
    if not storage_root.is_dir():
        return None

    spawn_targets = (
        _safe_resolve(child_cwd),
        _safe_resolve(runtime_root),
        _safe_resolve(runtime_root.parent),
    )
    matches: list[tuple[float, str]] = []
    candidates: list[tuple[float, str]] = []

    for candidate in iter_opencode_session_files(storage_root):
        session_id = opencode_session_id_from_path(candidate)
        if session_id is None:
            continue
        try:
            modified_at = candidate.stat().st_mtime
        except OSError:
            continue
        candidates.append((modified_at, session_id))

        try:
            payload = json.loads(candidate.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, json.JSONDecodeError):
            continue
        if _payload_matches_spawn(payload, spawn_targets):
            matches.append((modified_at, session_id))

    if matches:
        matches.sort(key=lambda item: item[0], reverse=True)
        return matches[0][1]

    if not candidates:
        return None

    started_at = _latest_spawn_start_epoch(runtime_root=runtime_root, child_cwd=child_cwd)
    if started_at is not None:
        lower = started_at - 5.0
        upper = started_at + float(_SESSION_MATCH_WINDOW_SECONDS)
        bounded = [item for item in candidates if item[0] >= lower and item[0] <= upper]
        if bounded:
            bounded.sort(key=lambda item: item[0], reverse=True)
            return bounded[0][1]

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


class OpenCodeHarnessExtractor(HarnessExtractor[OpenCodeLaunchSpec]):
    """Extractor implementation for OpenCode artifacts and events."""

    def detect_session_id_from_event(self, event: HarnessEvent) -> str | None:
        return session_from_mapping_with_keys(
            event.payload,
            ("session_id", "sessionId", "sessionID", "id"),
        )

    def detect_session_id_from_artifacts(
        self,
        *,
        spec: OpenCodeLaunchSpec,
        launch_env: Mapping[str, str],
        child_cwd: Path,
        runtime_root: Path,
    ) -> str | None:
        if spec.continue_session_id and spec.continue_session_id.strip():
            return spec.continue_session_id.strip()
        detected = _detect_primary_session_id(child_cwd=child_cwd, launch_env=launch_env)
        if detected:
            return detected
        return _detect_storage_session_id(
            launch_env=launch_env,
            child_cwd=child_cwd,
            runtime_root=runtime_root,
        )

    def extract_usage(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> TokenUsage:
        specific = _extract_opencode_usage(artifacts, spawn_id)
        if specific != TokenUsage():
            return specific
        return extract_usage_from_artifacts(artifacts, spawn_id)

    def extract_session_id(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        return extract_session_id_from_artifacts_with_patterns(
            artifacts,
            spawn_id,
            json_keys=("session_id", "sessionId", "sessionID", "id"),
            text_patterns=_SESSION_ID_TEXT_PATTERNS,
        )

    def extract_report(self, artifacts: ArtifactStore, spawn_id: SpawnId) -> str | None:
        return extract_opencode_report(artifacts, spawn_id)


OPENCODE_EXTRACTOR = OpenCodeHarnessExtractor()

__all__ = ["OPENCODE_EXTRACTOR", "OpenCodeHarnessExtractor"]


def _extract_opencode_usage(artifacts: ArtifactStore, spawn_id: SpawnId) -> TokenUsage:
    last: TokenUsage | None = None
    for payload in _iter_json_lines_artifact(artifacts, spawn_id, OUTPUT_FILENAME):
        event_type = (
            str(payload.get("event", payload.get("type", payload.get("event_type", ""))))
            .strip()
            .lower()
        )
        if event_type not in {"session.idle", "message.updated", "response.completed"}:
            continue
        info = payload.get("info") if isinstance(payload.get("info"), dict) else payload
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else info
        if not isinstance(usage, dict):
            continue
        cost_obj = payload.get("cost") if isinstance(payload.get("cost"), dict) else payload
        cost = coerce_optional_float(
            cost_obj.get("total_cost_usd") if isinstance(cost_obj, dict) else None
        )
        last = TokenUsage(
            input_tokens=_coerce_optional_int(usage.get("input_tokens") or usage.get("input")),
            output_tokens=_coerce_optional_int(usage.get("output_tokens") or usage.get("output")),
            cache_read_input_tokens=_coerce_optional_int(
                usage.get("cache_read_input_tokens") or usage.get("cache_read")
            ),
            cache_creation_input_tokens=_coerce_optional_int(
                usage.get("cache_creation_input_tokens") or usage.get("cache_write")
            ),
            reasoning_tokens=_coerce_optional_int(
                usage.get("reasoning_tokens") or usage.get("reasoning")
            ),
            total_cost_usd=cost,
        )
    return last or TokenUsage()
