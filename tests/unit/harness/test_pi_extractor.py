"""Pi extractor tests."""

from __future__ import annotations

import json
from pathlib import Path

from meridian.lib.core.types import ArtifactKey, SpawnId
from meridian.lib.harness.connections.base import HarnessEvent
from meridian.lib.harness.extractors.pi import PI_EXTRACTOR, _encode_cwd_for_session_dir
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
    session_dir = agent_dir / "sessions" / _encode_cwd_for_session_dir(child_cwd)
    session_dir.mkdir(parents=True)
    session_file = session_dir / "20260516_abc.jsonl"
    session_file.write_text(
        '{"type":"session","id":"ses-from-file"}\n{"type":"message"}\n',
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
    session_dir = agent_dir / "sessions" / _encode_cwd_for_session_dir(child_cwd)
    session_dir.mkdir(parents=True)
    session_file = session_dir / "20260516_fork.jsonl"
    session_file.write_text(
        '{"type":"session","id":"ses-fork-child"}\n',
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
