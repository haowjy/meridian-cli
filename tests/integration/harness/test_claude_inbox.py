"""Claude primary inbox route integration coverage."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from meridian.lib.core.delivery import DeliveryRefused, TransportDead
from meridian.lib.harness.bundle import PrimaryRouteContext
from meridian.lib.harness.claude_boundary import claude_boundary_journal_path
from meridian.lib.harness.claude_inbox import claude_primary_message_route
from meridian.lib.streaming.control_socket import socket_directory
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
    assert (
        claude_primary_message_route(PrimaryRouteContext(launch_paths(tmp_path, "p1"), os.getpid()))
        is None
    )


@pytest.mark.asyncio
async def test_route_reports_gone_socket_as_transport_dead(tmp_path: Path) -> None:
    # pytest's macOS tmp_path is too long for a socket address; Claude's inbox never is.
    _publish_inbox(tmp_path, socket_directory().parent / "missing.sock")
    route = claude_primary_message_route(
        PrimaryRouteContext(launch_paths(tmp_path, "p1"), os.getpid())
    )
    assert route is not None

    with pytest.raises(TransportDead, match="inbox is gone"):
        await route.deliver("unreachable", None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("inbox", "error"),
    [(Path("/tmp") / ("x" * 200) / "claude.sock", "too long"), (Path("claude.sock"), "relative")],
)
async def test_route_names_an_inbox_path_it_cannot_use(
    tmp_path: Path, inbox: Path, error: str
) -> None:
    _publish_inbox(tmp_path, inbox)
    route = claude_primary_message_route(
        PrimaryRouteContext(launch_paths(tmp_path, "p1"), os.getpid())
    )
    assert route is not None

    with pytest.raises(DeliveryRefused, match=f"{error}.*claude.sock|claude.sock.*{error}"):
        await route.deliver("unreachable", None)
