"""CLI routing coverage for primary message injection."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from meridian.cli import spawn_inject
from meridian.lib.state import spawn_store


@pytest.mark.asyncio
async def test_non_claude_primary_without_route_fails_immediately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spawn_store.start_spawn(
        tmp_path,
        spawn_id="p1",
        chat_id="c1",
        model="test-model",
        agent="test-agent",
        harness="codex",
        kind="primary",
        prompt="primary",
        worker_pid=12345,
    )
    monkeypatch.setattr(
        spawn_inject,
        "resolve_runtime_root_and_config",
        lambda _root: (tmp_path, object()),
    )
    monkeypatch.setattr(spawn_inject, "resolve_runtime_root", lambda _root: tmp_path)

    with pytest.raises(SystemExit):
        await asyncio.wait_for(
            spawn_inject.inject_message("p1", "hello"),
            timeout=1,
        )

    assert capsys.readouterr().err == (
        "Error: codex primaries have no inject route yet (see #498)\n"
    )
