"""Session log retrieval for primary spawns/chats with missing harness session IDs.

Tests that cover detection fallback (scanning native harness logs), primary_meta
reading, and error behavior when no session ID is recorded for primary spawns.

# qa-validated: test-suite-redesign
"""

import json
from pathlib import Path

from meridian.lib.launch.constants import HISTORY_FILENAME
from meridian.lib.ops.session_log import SessionLogInput, session_log_sync
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root_for_write


def _write_spawn_output(
    runtime_root: Path,
    spawn_id: str,
    *events: dict[str, object],
    filename: str = HISTORY_FILENAME,
) -> None:
    output_path = runtime_root / "spawns" / spawn_id / filename
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(json.dumps(event) for event in events) + "\n")


def test_session_log_primary_file_authority_needs_no_harness_session_id(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)

    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p42",
        chat_id="c42",
        model="gpt-5.4",
        agent="dev-orchestrator",
        harness="codex",
        kind="primary",
        prompt="do thing",
        harness_session_id="",
    )
    _write_spawn_output(
        runtime_root,
        "p42",
        {
            "event_type": "item/completed",
            "harness_id": "codex",
            "payload": {"item": {"type": "agentMessage", "text": "primary live progress"}},
        },
    )

    output = session_log_sync(
        SessionLogInput(ref="p42", project_root=project_root.as_posix(), tail=5)
    )
    assert [(message.role, message.content) for message in output.messages] == [
        ("assistant", "primary live progress")
    ]
