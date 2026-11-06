"""Claude primary inbox route integration coverage."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from meridian.lib.core.delivery import TransportDead
from meridian.lib.harness.claude_boundary import claude_boundary_journal_path
from meridian.lib.harness.claude_inbox import claude_primary_message_route
from tests.support.launch_paths import launch_paths


def _publish_inbox(runtime_root: Path, inbox: Path) -> None:
    journal = claude_boundary_journal_path(launch_paths(runtime_root, "p1"))
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(
        json.dumps(
            {
                "v": 2,
                "launch_nonce": "test",
                "pid": os.getpid(),
                "recorded_at": time.time(),
                "source": "startup",
                "session_id": "session",
                "transcript_path": str(runtime_root / "session.jsonl"),
                "messaging_socket": str(inbox),
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_route_waits_for_a_published_inbox(tmp_path: Path) -> None:
    assert claude_primary_message_route(launch_paths(tmp_path, "p1"), os.getpid()) is None


@pytest.mark.asyncio
async def test_route_reports_gone_socket_as_transport_dead(tmp_path: Path) -> None:
    _publish_inbox(tmp_path, tmp_path / "missing.sock")
    route = claude_primary_message_route(launch_paths(tmp_path, "p1"), os.getpid())
    assert route is not None

    with pytest.raises(TransportDead, match="inbox is gone"):
        await route.deliver("unreachable")
