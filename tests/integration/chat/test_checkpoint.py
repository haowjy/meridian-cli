from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import meridian.lib.chat.server as server
from meridian.lib.chat.protocol import ChatEvent, utc_now_iso
from meridian.lib.chat.server import app, configure
from meridian.lib.core.types import SpawnId
from tests.support.git import isolated_git_env


class Handle:
    spawn_id = SpawnId("p-checkpoint")

    def health(self) -> bool:
        return True

    async def send_message(self, text: str) -> None:
        pass

    async def send_cancel(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class Acquisition:
    async def acquire(
        self,
        chat_id: str,
        initial_prompt: str,
        *,
        execution_generation: int = 0,
        chat_state: str = "active",
    ) -> Handle:
        _ = (chat_id, initial_prompt, execution_generation, chat_state)
        return Handle()


pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git CLI is required")


def run(cmd: list[str], cwd: Path, *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, env=env, check=True, capture_output=True, text=True)

def test_turn_completed_callback_creates_checkpoint_and_revert_restores_file(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    git_env = isolated_git_env(global_config_path=tmp_path / "gitconfig")
    run(["git", "init"], project, env=git_env)
    repo_root = run(["git", "rev-parse", "--show-toplevel"], project, env=git_env)
    assert Path(repo_root.stdout.strip()).resolve() == project.resolve()
    run(["git", "config", "user.email", "test@example.com"], project, env=git_env)
    run(["git", "config", "user.name", "Test User"], project, env=git_env)
    tracked = project / "tracked.txt"
    tracked.write_text("base\n", encoding="utf-8")
    run(["git", "add", "tracked.txt"], project, env=git_env)
    run(["git", "commit", "-m", "base"], project, env=git_env)

    runtime = tmp_path / "runtime"
    configure(runtime_root=runtime, backend_acquisition=Acquisition(), project_root=project)
    with TestClient(app) as client:
        chat_id = client.post("/chat", json={}).json()["chat_id"]
        assert (
            client.post(f"/chat/{chat_id}/msg", json={"text": "hi"}).json()["status"]
            == "accepted"
        )

        tracked.write_text("changed\n", encoding="utf-8")
        pipeline = server._runtime.live_entries[chat_id].pipeline
        client.portal.call(
            pipeline.ingest,
            ChatEvent(
                type="turn.completed",
                seq=0,
                chat_id=chat_id,
                execution_id="p-checkpoint",
                timestamp=utc_now_iso(),
                turn_id="t1",
                payload={"turn_id": "t1", "execution_generation": 1},
            ),
        )
        client.portal.call(pipeline.drain)

        history = list(server._runtime.live_entries[chat_id].event_log.read_all())
        checkpoint_events = [event for event in history if event.type == "checkpoint.created"]
        assert len(checkpoint_events) == 1
        commit_sha = str(checkpoint_events[0].payload["commit_sha"])

        tracked.write_text("after\n", encoding="utf-8")
        revert = client.post(f"/chat/{chat_id}/revert", json={"commit_sha": commit_sha}).json()
        assert revert["status"] == "accepted"
        client.portal.call(pipeline.drain)

        history = list(server._runtime.live_entries[chat_id].event_log.read_all())

    assert tracked.read_text(encoding="utf-8") == "changed\n"
    assert [event.type for event in history if event.type.startswith("checkpoint.")] == [
        "checkpoint.created",
        "checkpoint.reverted",
    ]
