"""Composed Claude retry-safety regressions at the real subprocess boundary."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from meridian.lib.core.domain import Spawn
from meridian.lib.core.types import HarnessId, ModelId, SpawnId
from meridian.lib.harness.connections import claude_ws as claude_connection_module
from meridian.lib.harness.connections.errors import RetryableConnectionStartupError
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.launch import native_run as native_run_module
from meridian.lib.launch.request import RetryPolicy, SpawnRequest
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.artifact_store import LocalStore
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from meridian.lib.streaming import spawn_manager as spawn_manager_module
from tests.integration.launch.streaming_runner_support import (
    _execute_with_context,
    _FakeControlSocketServer,
)


@pytest.mark.parametrize(
    "upstream_error",
    [
        "subscription quota exhausted",
        "authentication rejected",
        "invalid model configuration",
    ],
)
@pytest.mark.asyncio
async def test_explicit_claude_terminal_result_is_not_replayed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    upstream_error: str,
) -> None:
    """A typed upstream terminal result must remain the one causal failure."""

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    invocation_log = tmp_path / "claude-invocations.jsonl"
    shim = bin_dir / "claude"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "2.0.0"; exit 0; fi\n'
        f"printf '%s\\n' \"$*\" >> {invocation_log}\n"
        f"invocation_count=$(wc -l < {invocation_log})\n"
        "session_id=\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        '  if [ "$1" = "--session-id" ]; then shift; session_id=$1; fi\n'
        "  shift\n"
        "done\n"
        "IFS= read -r prompt\n"
        "printf '{\"type\":\"system\",\"subtype\":\"init\","
        "\"session_id\":\"%s\"}\\n' \"$session_id\"\n"
        "if [ \"$invocation_count\" -gt 1 ]; then\n"
        "  printf '%s\\n' "
        "'{\"type\":\"result\",\"is_error\":true,"
        "\"result\":\"session id already in use\"}'\n"
        "else\n"
        "  printf '{\"type\":\"result\",\"subtype\":\"error_max_turns\","
        f"\"is_error\":true,\"result\":\"{upstream_error}\"}}\\n'\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _FakeControlSocketServer)

    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
    run = Spawn(
        spawn_id=SpawnId("r-claude-terminal-no-replay"),
        prompt="do work",
        model=ModelId("claude-sonnet-4-5"),
        status="queued",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id="test-chat-claude-terminal",
        model=str(run.model),
        agent="",
        harness=HarnessId.CLAUDE.value,
        kind="streaming",
        prompt=run.prompt,
        spawn_id=run.spawn_id,
        launch_mode="foreground",
        status="queued",
    )
    request = SpawnRequest(
        model=str(run.model),
        harness=HarnessId.CLAUDE.value,
        prompt=run.prompt,
        retry=RetryPolicy(max_attempts=2, backoff_secs=0.0),
    )

    exit_code = await asyncio.wait_for(
        _execute_with_context(
            run,
            request=request,
            project_root=tmp_path,
            runtime_root=runtime_root,
            artifacts=LocalStore(root_dir=tmp_path / ".artifacts"),
            registry=HarnessRegistry.with_defaults(),
        ),
        timeout=15.0,
    )

    row = spawn_store.get_spawn(runtime_root, run.spawn_id)
    assert exit_code == 1
    assert row is not None and row.terminal is not None
    assert row.terminal.error == upstream_error
    assert len(invocation_log.read_text(encoding="utf-8").splitlines()) == 1
    lifecycle_rows = [
        json.loads(line)
        for line in (runtime_root / "spawns" / str(run.spawn_id) / "runner-lifecycle.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [row["attempt"] for row in lifecycle_rows if row["event"] == "attempt_started"] == [
        1
    ]


@pytest.mark.asyncio
async def test_transient_before_process_and_prompt_retries_same_unconsumed_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    invocation_log = tmp_path / "claude-invocations"
    shim = bin_dir / "claude"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "2.0.0"; exit 0; fi\n'
        f"printf '%s\\n' \"$*\" >> {invocation_log}\n"
        "session_id=\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        '  if [ "$1" = "--session-id" ]; then shift; session_id=$1; fi\n'
        "  shift\n"
        "done\n"
        "IFS= read -r prompt\n"
        "printf '{\"type\":\"system\",\"subtype\":\"init\","
        "\"session_id\":\"%s\"}\\n' \"$session_id\"\n"
        "printf '%s\\n' "
        "'{\"type\":\"result\",\"subtype\":\"success\",\"is_error\":false,"
        "\"result\":\"completed\"}'\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _FakeControlSocketServer)

    real_launch = claude_connection_module.launch_managed_stdio
    launch_calls = 0

    async def fail_once_before_launch(**kwargs: object):
        nonlocal launch_calls
        launch_calls += 1
        if launch_calls == 1:
            raise RetryableConnectionStartupError("temporary launcher failure")
        return await real_launch(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        claude_connection_module,
        "launch_managed_stdio",
        fail_once_before_launch,
    )

    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
    run = Spawn(
        spawn_id=SpawnId("r-claude-safe-startup-retry"),
        prompt="do work",
        model=ModelId("claude-sonnet-4-5"),
        status="queued",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id="test-chat-claude-safe-startup",
        model=str(run.model),
        agent="",
        harness=HarnessId.CLAUDE.value,
        kind="streaming",
        prompt=run.prompt,
        spawn_id=run.spawn_id,
        launch_mode="foreground",
        status="queued",
    )
    request = SpawnRequest(
        model=str(run.model),
        harness=HarnessId.CLAUDE.value,
        prompt=run.prompt,
        retry=RetryPolicy(max_attempts=2, backoff_secs=0.0),
    )

    exit_code = await asyncio.wait_for(
        _execute_with_context(
            run,
            request=request,
            project_root=tmp_path,
            runtime_root=runtime_root,
            artifacts=LocalStore(root_dir=tmp_path / ".artifacts"),
            registry=HarnessRegistry.with_defaults(),
        ),
        timeout=15.0,
    )

    assert exit_code == 0
    assert launch_calls == 2
    argv = invocation_log.read_text(encoding="utf-8").splitlines()
    assert len(argv) == 1
    native_id = argv[0].split()[argv[0].split().index("--session-id") + 1]
    attempt_one = runtime_root / "spawns" / str(run.spawn_id) / "attempt-1"
    first_rows = [
        json.loads(line)
        for line in (attempt_one / "runner-lifecycle.jsonl").read_text().splitlines()
    ]
    assessment = next(row for row in first_rows if row["event"] == "retry_assessed")
    assert assessment["turn_submission"] == "not_submitted"
    assert assessment["native_create"] == "not_materialized"
    assert assessment["replay_safety"] == "proven_safe"
    assert assessment["retry"] is True
    assert assessment["planned_native_id"] == native_id
    row = spawn_store.get_spawn(runtime_root, run.spawn_id)
    assert row is not None and row.chat_id is not None
    chat = session_store.get_session_record(runtime_root, row.chat_id)
    assert chat is not None
    assert chat.harness_session_id == row.harness_session_id == native_id


@pytest.mark.asyncio
async def test_materialized_create_identity_blocks_pre_submission_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "claude"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "2.0.0"; exit 0; fi\n'
        "exit 99\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _FakeControlSocketServer)

    starts = 0

    async def materialize_then_fail(connection: object, config: object, spec: object) -> None:
        nonlocal starts
        _ = connection, config
        starts += 1
        identity = spec.native_identity  # type: ignore[attr-defined]
        assert identity is not None and identity.session_id is not None
        native_store = Path(identity.native_store)
        native_store.mkdir(parents=True, exist_ok=True)
        (native_store / f"{identity.session_id}.jsonl").write_text("partial", encoding="utf-8")
        raise RetryableConnectionStartupError("temporary failure after native create")

    monkeypatch.setattr(
        claude_connection_module.ClaudeConnection,
        "_start_subprocess",
        materialize_then_fail,
    )

    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
    run = Spawn(
        spawn_id=SpawnId("r-claude-materialized-no-retry"),
        prompt="do work",
        model=ModelId("claude-sonnet-4-5"),
        status="queued",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id="test-chat-claude-materialized",
        model=str(run.model),
        agent="",
        harness=HarnessId.CLAUDE.value,
        kind="streaming",
        prompt=run.prompt,
        spawn_id=run.spawn_id,
        launch_mode="foreground",
        status="queued",
    )

    exit_code = await asyncio.wait_for(
        _execute_with_context(
            run,
            request=SpawnRequest(
                model=str(run.model),
                harness=HarnessId.CLAUDE.value,
                prompt=run.prompt,
                retry=RetryPolicy(max_attempts=2, backoff_secs=0.0),
            ),
            project_root=tmp_path,
            runtime_root=runtime_root,
            artifacts=LocalStore(root_dir=tmp_path / ".artifacts"),
            registry=HarnessRegistry.with_defaults(),
        ),
        timeout=15.0,
    )

    assert exit_code == 2
    assert starts == 1
    row = spawn_store.get_spawn(runtime_root, run.spawn_id)
    assert row is not None and row.terminal is not None
    assert row.terminal.error == "temporary failure after native create"
    rows = [
        json.loads(line)
        for line in (runtime_root / "spawns" / str(run.spawn_id) / "runner-lifecycle.jsonl")
        .read_text()
        .splitlines()
    ]
    assessment = next(row for row in rows if row["event"] == "retry_assessed")
    assert assessment["turn_submission"] == "not_submitted"
    assert assessment["native_create"] == "materialized"
    assert assessment["replay_safety"] == "unsafe"
    assert assessment["retry"] is False


@pytest.mark.asyncio
async def test_unknown_prompt_send_progress_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    invocation_log = tmp_path / "claude-invocations"
    shim = bin_dir / "claude"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "2.0.0"; exit 0; fi\n'
        f"printf '%s\\n' \"$*\" >> {invocation_log}\n"
        "cat >/dev/null\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _FakeControlSocketServer)

    async def ambiguous_send(_connection: object, _text: str) -> None:
        raise RetryableConnectionStartupError("connection reset while writing prompt")

    monkeypatch.setattr(
        claude_connection_module.ClaudeConnection,
        "_send_user_turn",
        ambiguous_send,
    )

    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
    run = Spawn(
        spawn_id=SpawnId("r-claude-unknown-send-no-retry"),
        prompt="do work",
        model=ModelId("claude-sonnet-4-5"),
        status="queued",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id="test-chat-claude-unknown-send",
        model=str(run.model),
        agent="",
        harness=HarnessId.CLAUDE.value,
        kind="streaming",
        prompt=run.prompt,
        spawn_id=run.spawn_id,
        launch_mode="foreground",
        status="queued",
    )

    exit_code = await asyncio.wait_for(
        _execute_with_context(
            run,
            request=SpawnRequest(
                model=str(run.model),
                harness=HarnessId.CLAUDE.value,
                prompt=run.prompt,
                retry=RetryPolicy(max_attempts=2, backoff_secs=0.0),
            ),
            project_root=tmp_path,
            runtime_root=runtime_root,
            artifacts=LocalStore(root_dir=tmp_path / ".artifacts"),
            registry=HarnessRegistry.with_defaults(),
        ),
        timeout=15.0,
    )

    assert exit_code == 2
    assert len(invocation_log.read_text().splitlines()) == 1
    row = spawn_store.get_spawn(runtime_root, run.spawn_id)
    assert row is not None and row.terminal is not None
    assert row.terminal.error == "connection reset while writing prompt"
    rows = [
        json.loads(line)
        for line in (runtime_root / "spawns" / str(run.spawn_id) / "runner-lifecycle.jsonl")
        .read_text()
        .splitlines()
    ]
    assessment = next(row for row in rows if row["event"] == "retry_assessed")
    assert assessment["failure_disposition"] == "transient"
    assert assessment["turn_submission"] == "unknown"
    assert assessment["native_create"] == "not_materialized"
    assert assessment["replay_safety"] == "unknown"
    assert assessment["retry"] is False


@pytest.mark.parametrize("activity", ["disconnect", "model", "tool", "child"])
@pytest.mark.asyncio
async def test_submitted_claude_turn_is_never_replayed_after_transport_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    activity: str,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    invocation_log = tmp_path / "claude-invocations"
    side_effect = tmp_path / "tool-side-effect"
    shim = bin_dir / "claude"
    activity_frame = ""
    if activity == "model":
        activity_frame = (
            "printf '%s\\n' "
            "'{\"type\":\"assistant\",\"message\":{\"role\":\"assistant\","
            "\"content\":[{\"type\":\"text\",\"text\":\"draft\"}]}}'\n"
        )
    elif activity == "tool":
        activity_frame = (
            f"touch {side_effect}\n"
            "printf '%s\\n' "
            "'{\"type\":\"assistant\",\"message\":{\"role\":\"assistant\","
            "\"content\":[{\"type\":\"tool_use\",\"id\":\"tool-1\","
            "\"name\":\"Write\",\"input\":{}}]}}'\n"
        )
    elif activity == "child":
        activity_frame = (
            "printf '%s\\n' "
            "'{\"type\":\"assistant\",\"message\":{\"role\":\"assistant\","
            "\"content\":[{\"type\":\"text\",\"text\":\"launching child\"}]}}'\n"
        )
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "2.0.0"; exit 0; fi\n'
        f"printf '%s\\n' \"$*\" >> {invocation_log}\n"
        "session_id=\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        '  if [ "$1" = "--session-id" ]; then shift; session_id=$1; fi\n'
        "  shift\n"
        "done\n"
        "IFS= read -r prompt\n"
        "printf '{\"type\":\"system\",\"subtype\":\"init\","
        "\"session_id\":\"%s\"}\\n' \"$session_id\"\n"
        + activity_frame
        + "exit 1\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _FakeControlSocketServer)

    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
    run = Spawn(
        spawn_id=SpawnId(f"r-claude-close-{activity}"),
        prompt="do work",
        model=ModelId("claude-sonnet-4-5"),
        status="queued",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id=f"test-chat-claude-close-{activity}",
        model=str(run.model),
        agent="",
        harness=HarnessId.CLAUDE.value,
        kind="streaming",
        prompt=run.prompt,
        spawn_id=run.spawn_id,
        launch_mode="foreground",
        status="queued",
    )
    child_created = False

    def observe_event(_event: object) -> None:
        nonlocal child_created
        if activity != "child" or child_created:
            return
        child_created = True
        spawn_store.start_spawn(
            runtime_root,
            chat_id="test-chat-child-activity",
            parent_id=str(run.spawn_id),
            model=str(run.model),
            agent="",
            harness=HarnessId.CLAUDE.value,
            kind="streaming",
            prompt="child",
            spawn_id=SpawnId("r-claude-close-child-work"),
            launch_mode="background",
            status="running",
        )

    exit_code = await asyncio.wait_for(
        _execute_with_context(
            run,
            request=SpawnRequest(
                model=str(run.model),
                harness=HarnessId.CLAUDE.value,
                prompt=run.prompt,
                retry=RetryPolicy(max_attempts=2, backoff_secs=0.0),
            ),
            project_root=tmp_path,
            runtime_root=runtime_root,
            artifacts=LocalStore(root_dir=tmp_path / ".artifacts"),
            registry=HarnessRegistry.with_defaults(),
            event_observer=observe_event,
        ),
        timeout=15.0,
    )

    assert exit_code == 1
    assert len(invocation_log.read_text().splitlines()) == 1
    if activity == "tool":
        assert side_effect.exists()
    if activity == "child":
        assert child_created
        child = spawn_store.get_spawn(runtime_root, SpawnId("r-claude-close-child-work"))
        assert child is not None and child.parent_id == str(run.spawn_id)
    rows = [
        json.loads(line)
        for line in (runtime_root / "spawns" / str(run.spawn_id) / "runner-lifecycle.jsonl")
        .read_text()
        .splitlines()
    ]
    assessment = next(row for row in rows if row["event"] == "retry_assessed")
    assert assessment["failure_disposition"] == "transient"
    assert assessment["turn_submission"] == "submitted"
    assert assessment["replay_safety"] == "unsafe"
    assert assessment["retry"] is False


@pytest.mark.asyncio
async def test_cancellation_during_safe_retry_backoff_stops_next_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "claude"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "2.0.0"; exit 0; fi\n'
        "exit 99\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _FakeControlSocketServer)

    starts = 0

    async def transient_before_launch(_connection: object, _config: object, _spec: object) -> None:
        nonlocal starts
        starts += 1
        raise RetryableConnectionStartupError("temporary launcher failure")

    monkeypatch.setattr(
        claude_connection_module.ClaudeConnection,
        "_start_subprocess",
        transient_before_launch,
    )
    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
    run = Spawn(
        spawn_id=SpawnId("r-claude-cancel-backoff"),
        prompt="do work",
        model=ModelId("claude-sonnet-4-5"),
        status="queued",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id="test-chat-claude-cancel-backoff",
        model=str(run.model),
        agent="",
        harness=HarnessId.CLAUDE.value,
        kind="streaming",
        prompt=run.prompt,
        spawn_id=run.spawn_id,
        launch_mode="foreground",
        status="queued",
    )
    task = asyncio.create_task(
        _execute_with_context(
            run,
            request=SpawnRequest(
                model=str(run.model),
                harness=HarnessId.CLAUDE.value,
                prompt=run.prompt,
                retry=RetryPolicy(max_attempts=2, backoff_secs=5.0),
            ),
            project_root=tmp_path,
            runtime_root=runtime_root,
            artifacts=LocalStore(root_dir=tmp_path / ".artifacts"),
            registry=HarnessRegistry.with_defaults(),
        )
    )
    attempt_one = runtime_root / "spawns" / str(run.spawn_id) / "attempt-1"
    for _ in range(100):
        if attempt_one.is_dir():
            break
        await asyncio.sleep(0.02)
    assert attempt_one.is_dir()
    spawn_store.record_cancel_intent(
        runtime_root,
        run.spawn_id,
        exit_code=130,
        error="cancelled",
    )

    exit_code = await asyncio.wait_for(task, timeout=5.0)
    row = spawn_store.get_spawn(runtime_root, run.spawn_id)
    assert exit_code == 130
    assert starts == 1
    assert row is not None and row.terminal is not None
    assert row.terminal.error == "cancelled"


@pytest.mark.asyncio
async def test_retry_setup_failure_preserves_causal_attempt_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "claude"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "2.0.0"; exit 0; fi\n'
        "exit 99\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _FakeControlSocketServer)

    async def transient_before_launch(_connection: object, _config: object, _spec: object) -> None:
        raise RetryableConnectionStartupError("causal temporary launcher failure")

    def fail_rearm(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("retry plumbing exploded")

    monkeypatch.setattr(
        claude_connection_module.ClaudeConnection,
        "_start_subprocess",
        transient_before_launch,
    )
    monkeypatch.setattr(native_run_module.NativeRun, "rearm", fail_rearm)
    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
    run = Spawn(
        spawn_id=SpawnId("r-claude-retry-setup-cause"),
        prompt="do work",
        model=ModelId("claude-sonnet-4-5"),
        status="queued",
    )
    spawn_store.start_spawn(
        runtime_root,
        chat_id="test-chat-claude-retry-setup",
        model=str(run.model),
        agent="",
        harness=HarnessId.CLAUDE.value,
        kind="streaming",
        prompt=run.prompt,
        spawn_id=run.spawn_id,
        launch_mode="foreground",
        status="queued",
    )

    exit_code = await asyncio.wait_for(
        _execute_with_context(
            run,
            request=SpawnRequest(
                model=str(run.model),
                harness=HarnessId.CLAUDE.value,
                prompt=run.prompt,
                retry=RetryPolicy(max_attempts=2, backoff_secs=0.0),
            ),
            project_root=tmp_path,
            runtime_root=runtime_root,
            artifacts=LocalStore(root_dir=tmp_path / ".artifacts"),
            registry=HarnessRegistry.with_defaults(),
        ),
        timeout=15.0,
    )

    row = spawn_store.get_spawn(runtime_root, run.spawn_id)
    assert exit_code == 2
    assert row is not None and row.terminal is not None
    assert row.terminal.error == "causal temporary launcher failure"
    rows = [
        json.loads(line)
        for line in (runtime_root / "spawns" / str(run.spawn_id) / "runner-lifecycle.jsonl")
        .read_text()
        .splitlines()
    ]
    setup_failure = next(row for row in rows if row["event"] == "retry_setup_failed")
    assert setup_failure["exception"] == "retry plumbing exploded"
