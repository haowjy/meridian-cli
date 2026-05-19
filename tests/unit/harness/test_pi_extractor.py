# qa-validated: pi-rpc-quiescence
"""Pi extractor tests."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from meridian.lib.core.types import ArtifactKey, SpawnId
from meridian.lib.harness.connections.base import HarnessEvent
from meridian.lib.harness.extractors import pi as pi_extractor_module
from meridian.lib.harness.extractors.pi import (
    PI_EXTRACTOR,
    detect_pi_session_discovery_from_session_files,
    detect_pi_session_id_from_session_files,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver


class _MemoryArtifactStore:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self._payloads = payloads

    def get(self, key: ArtifactKey) -> bytes:
        return self._payloads[str(key)]

    def exists(self, key: ArtifactKey) -> bool:
        return str(key) in self._payloads


def _artifact_store_from_lines(
    spawn_id: SpawnId,
    lines: list[dict[str, object]],
) -> _MemoryArtifactStore:
    encoded = "\n".join(json.dumps(line) for line in lines).encode("utf-8")
    return _MemoryArtifactStore({f"{spawn_id}/output.jsonl": encoded})


def test_pi_extractor_reads_session_usage_and_report_from_output_jsonl() -> None:
    spawn_id = SpawnId("p-pi-extractor")
    store = _artifact_store_from_lines(
        spawn_id,
        [
            {
                "type": "session",
                "id": "019e3113-edc8-7751-bb29-9648304465d5",
            },
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "usage": {
                        "input": 123,
                        "output": 45,
                        "cacheRead": 7,
                        "cacheWrite": 8,
                    },
                },
            },
            {
                "type": "agent_end",
                "messages": [
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "final report"}],
                    }
                ],
            },
        ],
    )

    usage = PI_EXTRACTOR.extract_usage(store, spawn_id)

    assert (
        PI_EXTRACTOR.extract_session_id(store, spawn_id)
        == "019e3113-edc8-7751-bb29-9648304465d5"
    )
    assert usage.input_tokens == 123
    assert usage.output_tokens == 45
    assert usage.cache_read_input_tokens == 7
    assert usage.cache_creation_input_tokens == 8
    assert PI_EXTRACTOR.extract_report(store, spawn_id) == "final report"


def test_pi_extractor_detects_session_event_from_live_payload() -> None:
    event = HarnessEvent(
        event_type="session",
        payload={"id": "ses-pi-123"},
        harness_id="pi",
    )

    assert PI_EXTRACTOR.detect_session_id_from_event(event) == "ses-pi-123"


def test_pi_extractor_detects_session_id_from_pi_session_storage(tmp_path: Path) -> None:
    child_cwd = tmp_path / "repo"
    child_cwd.mkdir()
    agent_dir = tmp_path / "agent"
    session_dir = agent_dir / "sessions"
    session_dir.mkdir(parents=True)
    session_file = session_dir / "20260516_abc.jsonl"
    session_file.write_text(
        f'{{"type":"session","id":"ses-from-file","cwd":"{child_cwd}"}}\n{{"type":"message"}}\n',
        encoding="utf-8",
    )

    spec = ResolvedLaunchSpec(
        harness="pi",
        prompt="",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    detected = PI_EXTRACTOR.detect_session_id_from_artifacts(
        spec=spec,
        launch_env={"PI_CODING_AGENT_DIR": str(agent_dir)},
        child_cwd=child_cwd,
        runtime_root=tmp_path,
    )

    assert detected == "ses-from-file"


def test_pi_extractor_does_not_return_source_session_for_native_fork(tmp_path: Path) -> None:
    child_cwd = tmp_path / "repo"
    child_cwd.mkdir()
    agent_dir = tmp_path / "agent"
    session_dir = agent_dir / "sessions"
    session_dir.mkdir(parents=True)
    session_file = session_dir / "20260516_fork.jsonl"
    session_file.write_text(
        f'{{"type":"session","id":"ses-fork-child","cwd":"{child_cwd}"}}\n',
        encoding="utf-8",
    )

    spec = ResolvedLaunchSpec(
        harness="pi",
        prompt="",
        continue_session_id="ses-source",
        continue_fork=True,
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    detected = PI_EXTRACTOR.detect_session_id_from_artifacts(
        spec=spec,
        launch_env={"PI_CODING_AGENT_DIR": str(agent_dir)},
        child_cwd=child_cwd,
        runtime_root=tmp_path,
    )

    assert detected == "ses-fork-child"


def test_pi_extractor_prefers_pi_session_dir_env_override(tmp_path: Path) -> None:
    child_cwd = tmp_path / "repo"
    child_cwd.mkdir()
    session_root = tmp_path / "custom-sessions"
    session_root.mkdir(parents=True)
    (session_root / "20260516_custom.jsonl").write_text(
        f'{{"type":"session","id":"ses-from-session-dir-env","cwd":"{child_cwd}"}}\n',
        encoding="utf-8",
    )

    spec = ResolvedLaunchSpec(
        harness="pi",
        prompt="",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    detected = PI_EXTRACTOR.detect_session_id_from_artifacts(
        spec=spec,
        launch_env={"PI_CODING_AGENT_SESSION_DIR": str(session_root)},
        child_cwd=child_cwd,
        runtime_root=tmp_path,
    )

    assert detected == "ses-from-session-dir-env"


def test_pi_extractor_session_dir_override_ignores_stale_sibling_launches(tmp_path: Path) -> None:
    child_cwd = tmp_path / "repo"
    child_cwd.mkdir()
    parent_session_root = tmp_path / "custom-sessions"
    parent_session_root.mkdir(parents=True)
    stale_path = parent_session_root / "20260516_stale.jsonl"
    stale_path.write_text(
        f'{{"type":"session","id":"ses-stale-sibling","cwd":"{child_cwd}"}}\n',
        encoding="utf-8",
    )
    stale_time = time.time() - 3600.0
    os.utime(stale_path, (stale_time, stale_time))

    scoped_launch_root = parent_session_root
    (scoped_launch_root / "20260516_current.jsonl").write_text(
        f'{{"type":"session","id":"ses-current-launch","cwd":"{child_cwd}"}}\n',
        encoding="utf-8",
    )

    spec = ResolvedLaunchSpec(
        harness="pi",
        prompt="",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    detected = PI_EXTRACTOR.detect_session_id_from_artifacts(
        spec=spec,
        launch_env={"PI_CODING_AGENT_SESSION_DIR": str(scoped_launch_root)},
        child_cwd=child_cwd,
        runtime_root=tmp_path,
    )

    assert detected == "ses-current-launch"


def test_pi_extractor_uses_meridian_pi_sessions_default_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    child_cwd = tmp_path / "repo"
    child_cwd.mkdir()
    user_home = tmp_path / "user-home"
    session_root = user_home / "meridian-pi" / "sessions"
    session_root.mkdir(parents=True)
    (session_root / "20260516_default.jsonl").write_text(
        f'{{"type":"session","id":"ses-from-default-root","cwd":"{child_cwd}"}}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(pi_extractor_module, "get_user_home", lambda: user_home)

    spec = ResolvedLaunchSpec(
        harness="pi",
        prompt="",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    detected = PI_EXTRACTOR.detect_session_id_from_artifacts(
        spec=spec,
        launch_env={},
        child_cwd=child_cwd,
        runtime_root=tmp_path,
    )

    assert detected == "ses-from-default-root"


def test_detect_pi_session_id_from_session_files_prefers_new_recent_session_for_fork(
    tmp_path: Path,
) -> None:
    child_cwd = tmp_path / "repo"
    child_cwd.mkdir()
    session_root = tmp_path / "sessions"
    session_root.mkdir(parents=True)

    stale_path = session_root / "stale.jsonl"
    stale_path.write_text(
        f'{{"type":"session","id":"ses-source","cwd":"{child_cwd}"}}\n',
        encoding="utf-8",
    )
    stale_time = time.time() - 120.0
    os.utime(stale_path, (stale_time, stale_time))

    started_at = time.time()
    fresh_path = session_root / "fresh.jsonl"
    fresh_path.write_text(
        f'{{"type":"session","id":"ses-child","cwd":"{child_cwd}"}}\n',
        encoding="utf-8",
    )

    detected = detect_pi_session_id_from_session_files(
        launch_env={"PI_CODING_AGENT_SESSION_DIR": str(session_root)},
        child_cwd=child_cwd,
        started_at_epoch=started_at,
        expected_session_id="ses-source",
    )

    assert detected == "ses-child"


def test_pi_session_discovery_requires_matching_cwd(tmp_path: Path) -> None:
    child_cwd = tmp_path / "repo"
    child_cwd.mkdir()
    other_cwd = tmp_path / "other"
    other_cwd.mkdir()
    session_root = tmp_path / "sessions"
    session_root.mkdir(parents=True)
    (session_root / "other.jsonl").write_text(
        f'{{"type":"session","id":"ses-other","cwd":"{other_cwd}"}}\n',
        encoding="utf-8",
    )

    discovery = detect_pi_session_discovery_from_session_files(
        launch_env={"PI_CODING_AGENT_SESSION_DIR": str(session_root)},
        child_cwd=child_cwd,
        started_at_epoch=time.time(),
    )

    assert discovery.session_id is None
    assert discovery.discovery == "discovery_failed"
    assert discovery.detail is not None
    assert discovery.detail.startswith("no_matching_session: cwd=")


def test_pi_session_discovery_matches_windows_cwd_case_insensitively(
    tmp_path: Path,
    monkeypatch,
) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir(parents=True)
    child_cwd = Path("C:/Work/Repo")
    (session_root / "windows.jsonl").write_text(
        '{"type":"session","id":"ses-windows-case","cwd":"c:/work/repo"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(pi_extractor_module, "IS_WINDOWS", True)
    monkeypatch.setattr(
        pi_extractor_module,
        "_safe_resolve",
        lambda path: path.expanduser(),
    )

    discovery = detect_pi_session_discovery_from_session_files(
        launch_env={"PI_CODING_AGENT_SESSION_DIR": str(session_root)},
        child_cwd=child_cwd,
        started_at_epoch=time.time(),
    )

    assert discovery.session_id == "ses-windows-case"
    assert discovery.discovery == "ok"


def test_pi_session_discovery_reports_parse_errors_for_recent_flat_files(
    tmp_path: Path,
) -> None:
    child_cwd = tmp_path / "repo"
    child_cwd.mkdir()
    session_root = tmp_path / "sessions"
    session_root.mkdir(parents=True)
    (session_root / "20260518T010203_abc123.jsonl").write_text(
        "{bad-json\n",
        encoding="utf-8",
    )

    discovery = detect_pi_session_discovery_from_session_files(
        launch_env={"PI_CODING_AGENT_SESSION_DIR": str(session_root)},
        child_cwd=child_cwd,
        started_at_epoch=time.time(),
    )

    assert discovery.session_id is None
    assert discovery.discovery == "discovery_failed"
    assert discovery.detail is not None
    assert discovery.detail.startswith("session_file_parse_error:")


def test_pi_session_discovery_ignores_stale_parse_error_files(tmp_path: Path) -> None:
    child_cwd = tmp_path / "repo"
    child_cwd.mkdir()
    other_cwd = tmp_path / "other"
    other_cwd.mkdir()
    session_root = tmp_path / "sessions"
    session_root.mkdir(parents=True)
    stale_path = session_root / "20260518T010203_broken.jsonl"
    stale_path.write_text("{bad-json\n", encoding="utf-8")
    stale_time = time.time() - 3600.0
    os.utime(stale_path, (stale_time, stale_time))
    (session_root / "other.jsonl").write_text(
        f'{{"type":"session","id":"ses-other","cwd":"{other_cwd}"}}\n',
        encoding="utf-8",
    )

    discovery = detect_pi_session_discovery_from_session_files(
        launch_env={"PI_CODING_AGENT_SESSION_DIR": str(session_root)},
        child_cwd=child_cwd,
        started_at_epoch=time.time(),
    )

    assert discovery.session_id is None
    assert discovery.discovery == "discovery_failed"
    assert discovery.detail is not None
    assert discovery.detail.startswith("no_matching_session: cwd=")


def test_pi_session_discovery_reports_open_errors_for_recent_flat_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    child_cwd = tmp_path / "repo"
    child_cwd.mkdir()
    session_root = tmp_path / "sessions"
    session_root.mkdir(parents=True)
    blocked = session_root / "20260518T010203_blocked.jsonl"
    blocked.write_text(
        f'{{"type":"session","id":"ses-unreadable","cwd":"{child_cwd}"}}\n',
        encoding="utf-8",
    )

    original_open = Path.open

    def _raise_for_blocked(path: Path, *args: Any, **kwargs: Any):
        if path == blocked:
            raise OSError("permission denied")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _raise_for_blocked)

    discovery = detect_pi_session_discovery_from_session_files(
        launch_env={"PI_CODING_AGENT_SESSION_DIR": str(session_root)},
        child_cwd=child_cwd,
        started_at_epoch=time.time(),
    )

    assert discovery.session_id is None
    assert discovery.discovery == "discovery_failed"
    assert discovery.detail is not None
    assert discovery.detail.startswith("session_file_parse_error:")
    assert "permission denied" in discovery.detail


def test_pi_session_discovery_reports_never_created_when_dir_missing(tmp_path: Path) -> None:
    discovery = detect_pi_session_discovery_from_session_files(
        launch_env={"PI_CODING_AGENT_SESSION_DIR": str(tmp_path / "missing")},
        child_cwd=tmp_path,
    )

    assert discovery.session_id is None
    assert discovery.discovery == "never_created"
    assert discovery.detail is not None
    assert discovery.detail.startswith("session_dir_missing:")


def test_pi_session_discovery_ignores_expected_id_without_new_match(tmp_path: Path) -> None:
    child_cwd = tmp_path / "repo"
    child_cwd.mkdir()
    session_root = tmp_path / "sessions"
    session_root.mkdir(parents=True)
    (session_root / "expected.jsonl").write_text(
        f'{{"type":"session","id":"ses-source","cwd":"{child_cwd}"}}\n',
        encoding="utf-8",
    )

    discovery = detect_pi_session_discovery_from_session_files(
        launch_env={"PI_CODING_AGENT_SESSION_DIR": str(session_root)},
        child_cwd=child_cwd,
        started_at_epoch=time.time(),
        expected_session_id="ses-source",
    )

    assert discovery.session_id is None
    assert discovery.discovery == "discovery_failed"
