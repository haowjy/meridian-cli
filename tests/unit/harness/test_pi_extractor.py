# qa-validated: pi-rpc-quiescence
"""Pi extractor tests."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from meridian.lib.core.types import ArtifactKey, SpawnId
from meridian.lib.harness.extractors import pi as pi_extractor_module
from meridian.lib.harness.extractors.pi import (
    PI_EXTRACTOR,
    detect_pi_session_discovery_from_session_files,
    detect_pi_session_id_from_session_files,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver


def _session_jsonl(session_id: str, cwd: Path, *, extra_lines: str = "") -> str:
    """Build a JSONL session file content with properly escaped paths.

    Using json.dumps ensures Windows backslash paths are correctly escaped.
    """
    line = json.dumps({"type": "session", "id": session_id, "cwd": str(cwd)})
    result = line + "\n"
    if extra_lines:
        result += extra_lines
    return result


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


@pytest.mark.parametrize(
    ("storage_kind", "expected_session_id"),
    [
        ("agent_dir", "ses-from-agent-dir"),
        ("session_dir", "ses-from-session-dir-env"),
        ("default_root", "ses-from-default-root"),
    ],
)
def test_pi_extractor_detects_session_id_from_supported_storage_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    storage_kind: str,
    expected_session_id: str,
) -> None:
    child_cwd = tmp_path / "repo"
    child_cwd.mkdir()
    launch_env: dict[str, str] = {}

    if storage_kind == "agent_dir":
        agent_dir = tmp_path / "agent"
        session_root = agent_dir / "sessions"
        launch_env["PI_CODING_AGENT_DIR"] = str(agent_dir)
    elif storage_kind == "session_dir":
        session_root = tmp_path / "custom-sessions"
        launch_env["PI_CODING_AGENT_SESSION_DIR"] = str(session_root)
    else:
        user_home = tmp_path / "user-home"
        session_root = user_home / "meridian-pi" / "sessions"
        monkeypatch.setattr(pi_extractor_module, "get_user_home", lambda: user_home)

    session_root.mkdir(parents=True)
    (session_root / "20260516_current.jsonl").write_text(
        _session_jsonl(expected_session_id, child_cwd, extra_lines='{"type":"message"}\n'),
        encoding="utf-8",
    )

    spec = ResolvedLaunchSpec(
        harness="pi",
        prompt="",
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )

    detected = PI_EXTRACTOR.detect_session_id_from_artifacts(
        spec=spec,
        launch_env=launch_env,
        child_cwd=child_cwd,
        runtime_root=tmp_path,
    )

    assert detected == expected_session_id


def test_pi_extractor_detects_new_fork_session_instead_of_source(tmp_path: Path) -> None:
    child_cwd = tmp_path / "repo"
    child_cwd.mkdir()
    session_root = tmp_path / "sessions"
    session_root.mkdir(parents=True)

    stale_path = session_root / "stale.jsonl"
    stale_path.write_text(_session_jsonl("ses-source", child_cwd), encoding="utf-8")
    stale_time = time.time() - 120.0
    os.utime(stale_path, (stale_time, stale_time))

    started_at = time.time()
    (session_root / "fresh.jsonl").write_text(
        _session_jsonl("ses-child", child_cwd),
        encoding="utf-8",
    )

    detected = detect_pi_session_id_from_session_files(
        launch_env={"PI_CODING_AGENT_SESSION_DIR": str(session_root)},
        child_cwd=child_cwd,
        started_at_epoch=started_at,
        expected_session_id="ses-source",
    )

    assert detected == "ses-child"


def test_pi_session_discovery_matches_windows_cwd_case_insensitively(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir(parents=True)
    child_cwd = Path("C:/Work/Repo")
    (session_root / "windows.jsonl").write_text(
        '{"type":"session","id":"ses-windows-case","cwd":"c:/work/repo"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(pi_extractor_module, "IS_WINDOWS", True)

    discovery = detect_pi_session_discovery_from_session_files(
        launch_env={"PI_CODING_AGENT_SESSION_DIR": str(session_root)},
        child_cwd=child_cwd,
        started_at_epoch=time.time(),
    )

    assert discovery.session_id == "ses-windows-case"
    assert discovery.discovery == "ok"


@pytest.mark.parametrize(
    "case",
    ["no_matching_cwd", "recent_parse_error", "stale_parse_error", "missing_dir", "expected_id"],
)
def test_pi_session_discovery_reports_unsuccessful_discovery_reasons(
    tmp_path: Path,
    case: str,
) -> None:
    child_cwd = tmp_path / "repo"
    child_cwd.mkdir()
    launch_env: dict[str, str]
    expected_detail_prefix: str
    expected_discovery = "discovery_failed"
    expected_id: str | None = None

    if case == "missing_dir":
        launch_env = {"PI_CODING_AGENT_SESSION_DIR": str(tmp_path / "missing")}
        expected_discovery = "never_created"
        expected_detail_prefix = "session_dir_missing:"
    else:
        other_cwd = tmp_path / "other"
        other_cwd.mkdir()
        session_root = tmp_path / "sessions"
        session_root.mkdir(parents=True)
        launch_env = {"PI_CODING_AGENT_SESSION_DIR": str(session_root)}
        expected_detail_prefix = "no_matching_session: cwd="

        if case == "no_matching_cwd":
            (session_root / "other.jsonl").write_text(
                _session_jsonl("ses-other", other_cwd),
                encoding="utf-8",
            )
        elif case == "recent_parse_error":
            (session_root / "20260518T010203_abc123.jsonl").write_text(
                "{bad-json\n",
                encoding="utf-8",
            )
            expected_detail_prefix = "session_file_parse_error:"
        elif case == "stale_parse_error":
            stale_path = session_root / "20260518T010203_broken.jsonl"
            stale_path.write_text("{bad-json\n", encoding="utf-8")
            stale_time = time.time() - 3600.0
            os.utime(stale_path, (stale_time, stale_time))
            (session_root / "other.jsonl").write_text(
                _session_jsonl("ses-other", other_cwd),
                encoding="utf-8",
            )
        elif case == "expected_id":
            (session_root / "expected.jsonl").write_text(
                _session_jsonl("ses-source", child_cwd),
                encoding="utf-8",
            )
            expected_id = "ses-source"

    discovery = detect_pi_session_discovery_from_session_files(
        launch_env=launch_env,
        child_cwd=child_cwd,
        started_at_epoch=time.time(),
        expected_session_id=expected_id,
    )

    assert discovery.session_id is None
    assert discovery.discovery == expected_discovery
    assert discovery.detail is not None
    assert discovery.detail.startswith(expected_detail_prefix)
