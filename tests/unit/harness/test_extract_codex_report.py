import json
from pathlib import Path

from meridian.lib.core.types import ArtifactKey, SpawnId
from meridian.lib.harness.common import extract_codex_report
from meridian.lib.state.artifact_store import LocalStore


def _write_history(local_store: LocalStore, spawn_id: SpawnId, records: list[object]) -> None:
    lines = "\n".join(json.dumps(record) for record in records)
    local_store.put(ArtifactKey(f"{spawn_id}/history.jsonl"), f"{lines}\n".encode())


def test_extract_codex_report_keeps_final_agent_message(tmp_path: Path) -> None:
    artifacts = LocalStore(root_dir=tmp_path / "artifacts")
    spawn_id = SpawnId("p-codex-report")
    _write_history(
        artifacts,
        spawn_id,
        [
            {
                "event_type": "item/completed",
                "payload": {"item": {"type": "agentMessage", "text": "Done."}},
            }
        ],
    )

    assert extract_codex_report(artifacts, spawn_id) == "Done."


def test_extract_codex_report_rejects_message_before_started_command(tmp_path: Path) -> None:
    artifacts = LocalStore(root_dir=tmp_path / "artifacts")
    spawn_id = SpawnId("p-codex-cancel")
    _write_history(
        artifacts,
        spawn_id,
        [
            {
                "event_type": "item/completed",
                "payload": {
                    "item": {
                        "type": "agentMessage",
                        "text": "Running `sleep 600` now and will wait.",
                    }
                },
            },
            {
                "event_type": "item/started",
                "payload": {
                    "item": {
                        "type": "commandExecution",
                        "command": "/bin/bash -lc 'sleep 600'",
                    }
                },
            },
            {
                "event_type": "meridian/error/connectionClosed",
                "payload": {"message": "no close frame received or sent"},
            },
        ],
    )

    assert extract_codex_report(artifacts, spawn_id) is None
