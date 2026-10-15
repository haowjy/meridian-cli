"""Static contracts for distinct persisted session identities."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_chat_and_harness_session_ids_cannot_be_swapped(tmp_path: Path) -> None:
    source = tmp_path / "identity_swap.py"
    source.write_text(
        "\n".join(
            (
                "from meridian.lib.core.types import ChatId, HarnessSessionId",
                'chat_id: ChatId = ChatId("c1")',
                'harness_session_id: HarnessSessionId = HarnessSessionId("thread-1")',
                "chat_id = harness_session_id",
            )
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-m", "pyright", str(source)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert 'Type "HarnessSessionId" is not assignable to declared type "ChatId"' in result.stdout


def test_run_id_cannot_be_used_as_a_stable_spawn_id(tmp_path: Path) -> None:
    source = tmp_path / "run_is_not_spawn.py"
    source.write_text(
        "\n".join(
            (
                "from meridian.lib.core.types import RunId, SpawnId",
                'run_id: RunId = RunId("p1.2")',
                "spawn_id: SpawnId = run_id",
            )
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-m", "pyright", str(source)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert 'Type "RunId" is not assignable to declared type "SpawnId"' in result.stdout
