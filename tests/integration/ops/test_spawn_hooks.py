from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.core.lifecycle import create_lifecycle_service
from meridian.lib.launch.types import PrimarySessionMetadata

if TYPE_CHECKING:
    import pytest


def _python_command(script_path: Path, *args: str) -> str:
    return subprocess.list2cmdline([sys.executable, str(script_path), *args])


def _write_hook_recorder(path: Path) -> None:
    path.write_text(
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        "payload = json.loads(sys.stdin.read())\n"
        "target = Path(sys.argv[1])\n"
        "target.parent.mkdir(parents=True, exist_ok=True)\n"
        "with target.open('a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(payload) + '\\n')\n",
        encoding="utf-8",
    )


def test_spawn_lifecycle_dispatches_spawn_hooks_with_expected_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".git").mkdir()
    runtime_root = project_root / ".meridian"

    user_config = tmp_path / "user-config.toml"
    user_config.write_text("", encoding="utf-8")
    monkeypatch.setenv("MERIDIAN_CONFIG", user_config.as_posix())
    monkeypatch.setenv("MERIDIAN_HOOKS_ENABLED", "true")

    marker = tmp_path / "spawn-hook-events.jsonl"
    recorder = tmp_path / "record_hook.py"
    _write_hook_recorder(recorder)
    command = _python_command(recorder, marker.as_posix())

    (project_root / "meridian.toml").write_text(
        f"[[hooks]]\n"
        f"name = 'record-created'\n"
        f"event = 'spawn.created'\n"
        f"command = '{command}'\n\n"
        f"[[hooks]]\n"
        f"name = 'record-finalized'\n"
        f"event = 'spawn.finalized'\n"
        f"command = '{command}'\n",
        encoding="utf-8",
    )

    lifecycle = create_lifecycle_service(project_root, runtime_root)
    spawn_id = lifecycle.start(
        chat_id="chat-1",
        session_metadata=PrimarySessionMetadata(
            harness="codex",
            model="gpt-5.4",
            agent="reviewer",
            agent_path="",
            skills=(),
            skill_paths=(),
        ),
        kind="child",
        prompt="hook-test",
        status="running",
        work_id="hook-work",
    )
    lifecycle.finalize(
        spawn_id,
        status="succeeded",
        exit_code=0,
        origin="runner",
        duration_secs=3.0,
        total_cost_usd=0.25,
    )

    payloads = [json.loads(line) for line in marker.read_text(encoding="utf-8").splitlines()]
    assert len(payloads) == 2
    by_event = {payload["event_name"]: payload for payload in payloads}

    created = by_event["spawn.created"]
    assert created["spawn"]["id"] == spawn_id
    assert created["spawn"]["agent"] == "reviewer"
    assert created["spawn"]["model"] == "gpt-5.4"
    assert created["work"]["id"] == "hook-work"
    assert created["project_root"] == project_root.resolve().as_posix()
    assert created["runtime_root"] == runtime_root.resolve().as_posix()

    finalized = by_event["spawn.finalized"]
    assert finalized["spawn"]["id"] == spawn_id
    assert finalized["spawn"]["status"] == "success"
    assert finalized["spawn"]["duration_secs"] == 3.0
    assert finalized["spawn"]["cost_usd"] == 0.25
    assert finalized["work"]["id"] == "hook-work"
