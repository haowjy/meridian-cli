"""Composed Claude retry-safety regressions at the real subprocess boundary."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from meridian.lib.core.domain import Spawn
from meridian.lib.core.types import HarnessId, ModelId, SpawnId
from meridian.lib.harness.adapter import StreamEvent
from meridian.lib.harness.claude_sessions import project_slug
from meridian.lib.harness.connections import claude_ws as claude_connection_module
from meridian.lib.harness.connections.errors import (
    IncompleteStartupTeardown,
    RetryableConnectionStartupError,
    TeardownStatus,
)
from meridian.lib.harness.connections.managed_stdio import ManagedStdioProcess
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


@dataclass
class _ClaudeScenario:
    """Concise local builder for one isolated fake-Claude runner scenario."""

    root: Path
    monkeypatch: pytest.MonkeyPatch

    def __post_init__(self) -> None:
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.invocation_log = self.root / "claude-invocations"
        self.runtime_root = resolve_project_runtime_root_for_write(self.root)
        self.artifacts = LocalStore(root_dir=self.root / ".artifacts")
        self.monkeypatch.setenv("PATH", f"{self.bin_dir}:{os.environ.get('PATH', '')}")
        self.monkeypatch.setenv("HOME", str(self.root / "home"))
        self.monkeypatch.setattr(
            spawn_manager_module,
            "ControlSocketServer",
            _FakeControlSocketServer,
        )

    def install_claude(self, body: str) -> None:
        shim = self.bin_dir / "claude"
        shim.write_text(
            '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "2.0.0"; exit 0; fi\n' + body,
            encoding="utf-8",
        )
        shim.chmod(0o755)

    async def run(
        self,
        name: str,
        *,
        retry: RetryPolicy | None = None,
        event_observer: Callable[[StreamEvent], None] | None = None,
    ) -> tuple[int, Spawn]:
        run = Spawn(
            spawn_id=SpawnId(f"r-claude-{name}"),
            prompt="do work",
            model=ModelId("claude-sonnet-4-5"),
            status="queued",
        )
        spawn_store.start_spawn(
            self.runtime_root,
            chat_id=f"test-chat-claude-{name}",
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
                    retry=retry or RetryPolicy(max_attempts=2, backoff_secs=0.0),
                ),
                project_root=self.root,
                runtime_root=self.runtime_root,
                artifacts=self.artifacts,
                registry=HarnessRegistry.with_defaults(),
                event_observer=event_observer,
            ),
            timeout=15.0,
        )
        return exit_code, run

    def lifecycle(self, run: Spawn, *, attempt: int | None = None) -> list[dict[str, object]]:
        directory = self.runtime_root / "spawns" / str(run.spawn_id)
        if attempt is not None:
            directory /= f"attempt-{attempt}"
        return [
            json.loads(line)
            for line in (directory / "runner-lifecycle.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]

    def invocations(self) -> list[str]:
        if not self.invocation_log.exists():
            return []
        return self.invocation_log.read_text(encoding="utf-8").splitlines()

    @staticmethod
    def session_id(argv: str) -> str:
        parts = shlex.split(argv)
        return parts[parts.index("--session-id") + 1]

    def native_path(self, session_id: str) -> Path:
        return (
            self.root
            / "home"
            / ".claude"
            / "projects"
            / project_slug(self.root)
            / f"{session_id}.jsonl"
        )


@pytest.fixture
def claude_scenario(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> _ClaudeScenario:
    return _ClaudeScenario(tmp_path, monkeypatch)


@pytest.mark.parametrize(
    "upstream_error",
    [
        "subscription quota exhausted",
        "authentication rejected",
        "invalid model configuration",
        "prompt too long: reduce input",
    ],
)
@pytest.mark.asyncio
async def test_explicit_claude_terminal_result_is_not_replayed(
    claude_scenario: _ClaudeScenario,
    upstream_error: str,
) -> None:
    """Typed terminal cause wins even when its text matches a legacy marker."""

    log = shlex.quote(str(claude_scenario.invocation_log))
    claude_scenario.install_claude(
        f"printf '%s\\n' \"$*\" >> {log}\n"
        "session_id=\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "--session-id" ]; then shift; session_id=$1; fi\n'
        "  shift\n"
        "done\n"
        "IFS= read -r prompt\n"
        'printf \'{"type":"system","subtype":"init",'
        '"session_id":"%s"}\\n\' "$session_id"\n'
        'printf \'{"type":"result","subtype":"error_max_turns",'
        f'"is_error":true,"result":"{upstream_error}"}}\\n\'\n'
        "exit 1\n"
    )

    exit_code, run = await claude_scenario.run("terminal-no-replay")

    row = spawn_store.get_spawn(claude_scenario.runtime_root, run.spawn_id)
    assert exit_code == 1
    assert row is not None and row.terminal is not None
    assert row.terminal.error == upstream_error
    assert len(claude_scenario.invocations()) == 1
    assert [
        item["attempt"]
        for item in claude_scenario.lifecycle(run)
        if item["event"] == "attempt_started"
    ] == [1]


@pytest.mark.asyncio
async def test_transient_before_process_retries_same_unconsumed_identity(
    claude_scenario: _ClaudeScenario,
) -> None:
    log = shlex.quote(str(claude_scenario.invocation_log))
    claude_scenario.install_claude(
        f"printf '%s\\n' \"$*\" >> {log}\n"
        "session_id=\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "--session-id" ]; then shift; session_id=$1; fi\n'
        "  shift\n"
        "done\n"
        "IFS= read -r prompt\n"
        'printf \'{"type":"system","subtype":"init",'
        '"session_id":"%s"}\\n\' "$session_id"\n'
        'printf \'%s\\n\' \'{"type":"result","subtype":"success",'
        '"is_error":false,"result":"completed"}\'\n'
    )
    real_launch = claude_connection_module.launch_managed_stdio
    launch_calls = 0

    async def fail_once_before_launch(**kwargs: object):
        nonlocal launch_calls
        launch_calls += 1
        if launch_calls == 1:
            raise RetryableConnectionStartupError("temporary launcher failure")
        return await real_launch(**kwargs)  # type: ignore[arg-type]

    claude_scenario.monkeypatch.setattr(
        claude_connection_module,
        "launch_managed_stdio",
        fail_once_before_launch,
    )

    exit_code, run = await claude_scenario.run("safe-startup-retry")

    assert exit_code == 0
    assert launch_calls == 2
    invocations = claude_scenario.invocations()
    assert len(invocations) == 1
    native_id = claude_scenario.session_id(invocations[0])
    assessment = next(
        row for row in claude_scenario.lifecycle(run, attempt=1) if row["event"] == "retry_assessed"
    )
    assert assessment["turn_submission"] == "not_submitted"
    assert assessment["native_create"] == "not_materialized"
    assert assessment["replay_safety"] == "proven_safe"
    assert assessment["retry"] is True
    assert assessment["planned_native_id"] == native_id
    row = spawn_store.get_spawn(claude_scenario.runtime_root, run.spawn_id)
    assert row is not None and row.chat_id is not None
    chat = session_store.get_session_record(claude_scenario.runtime_root, row.chat_id)
    assert chat is not None
    assert chat.harness_session_id == row.harness_session_id == native_id


@pytest.mark.asyncio
async def test_incomplete_teardown_with_delayed_materialization_never_retries(
    claude_scenario: _ClaudeScenario,
) -> None:
    """An absent path is not proof while the first process can still create it."""

    log = shlex.quote(str(claude_scenario.invocation_log))
    claude_scenario.install_claude(
        f"printf '%s\\n' \"$*\" >> {log}\n"
        "session_id=\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "--session-id" ]; then shift; session_id=$1; fi\n'
        "  shift\n"
        "done\n"
        'if [ "$ATTEMPT_NUMBER" = "1" ]; then\n'
        "  sleep 0.2\n"
        '  mkdir -p "$(dirname "$NATIVE_PATH")"\n'
        '  printf partial > "$NATIVE_PATH"\n'
        "  sleep 30\n"
        "fi\n"
        "IFS= read -r prompt\n"
        'printf \'{"type":"system","subtype":"init",'
        '"session_id":"%s"}\\n\' "$session_id"\n'
        'printf \'%s\\n\' \'{"type":"result","is_error":true,'
        '"result":"session id already in use"}\'\n'
        "exit 1\n"
    )
    real_launch = claude_connection_module.launch_managed_stdio
    first_child: list[ManagedStdioProcess] = []
    launch_calls = 0

    async def abandon_first_child(**kwargs: object):
        nonlocal launch_calls
        launch_calls += 1
        command = kwargs["command"]
        assert isinstance(command, list)
        native_id = command[command.index("--session-id") + 1]
        native_path = claude_scenario.native_path(native_id)
        env = dict(kwargs["env"])  # type: ignore[arg-type]
        env.update(ATTEMPT_NUMBER=str(launch_calls), NATIVE_PATH=str(native_path))
        kwargs["env"] = env
        if launch_calls > 1:
            for _ in range(100):
                if native_path.exists():
                    break
                await asyncio.sleep(0.01)
        child = await real_launch(**kwargs)  # type: ignore[arg-type]
        if launch_calls == 1:
            first_child.append(child)
            raise IncompleteStartupTeardown(
                RetryableConnectionStartupError("temporary startup handoff failure"),
                teardown=TeardownStatus.ABANDONED,
            )
        return child

    claude_scenario.monkeypatch.setattr(
        claude_connection_module,
        "launch_managed_stdio",
        abandon_first_child,
    )

    try:
        exit_code, run = await claude_scenario.run("abandoned-no-retry")
        invocations = claude_scenario.invocations()
        assert exit_code == 2
        assert len(invocations) == 1
        native_id = claude_scenario.session_id(invocations[0])
        native_path = claude_scenario.native_path(native_id)
        for _ in range(100):
            if native_path.exists():
                break
            await asyncio.sleep(0.01)
        assert native_path.exists()
        row = spawn_store.get_spawn(claude_scenario.runtime_root, run.spawn_id)
        assert row is not None and row.terminal is not None
        assert row.terminal.error == "temporary startup handoff failure"
        assessment = next(
            item for item in claude_scenario.lifecycle(run) if item["event"] == "retry_assessed"
        )
        assert assessment["teardown"] == "abandoned"
        assert assessment["native_create"] == "unknown"
        assert assessment["replay_safety"] == "unknown"
        assert assessment["retry"] is False
        assert assessment["planned_native_id"] == native_id
        assert row.harness_session_id == native_id
    finally:
        for child in first_child:
            await child.terminate()


@pytest.mark.asyncio
async def test_materialized_create_blocks_pre_submission_retry(
    claude_scenario: _ClaudeScenario,
) -> None:
    claude_scenario.install_claude("exit 99\n")
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

    claude_scenario.monkeypatch.setattr(
        claude_connection_module.ClaudeConnection,
        "_start_subprocess",
        materialize_then_fail,
    )

    exit_code, run = await claude_scenario.run("materialized-no-retry")

    assert exit_code == 2
    assert starts == 1
    row = spawn_store.get_spawn(claude_scenario.runtime_root, run.spawn_id)
    assert row is not None and row.terminal is not None
    assert row.terminal.error == "temporary failure after native create"
    assessment = next(
        item for item in claude_scenario.lifecycle(run) if item["event"] == "retry_assessed"
    )
    assert assessment["turn_submission"] == "not_submitted"
    assert assessment["native_create"] == "materialized"
    assert assessment["replay_safety"] == "unsafe"
    assert assessment["retry"] is False


@pytest.mark.asyncio
async def test_unknown_prompt_send_progress_fails_closed(
    claude_scenario: _ClaudeScenario,
) -> None:
    log = shlex.quote(str(claude_scenario.invocation_log))
    claude_scenario.install_claude(f"printf '%s\\n' \"$*\" >> {log}\ncat >/dev/null\n")

    async def ambiguous_send(_connection: object, _text: str) -> None:
        raise RetryableConnectionStartupError("connection reset while writing prompt")

    claude_scenario.monkeypatch.setattr(
        claude_connection_module.ClaudeConnection,
        "_send_user_turn",
        ambiguous_send,
    )

    exit_code, run = await claude_scenario.run("unknown-send-no-retry")

    assert exit_code == 2
    assert len(claude_scenario.invocations()) == 1
    row = spawn_store.get_spawn(claude_scenario.runtime_root, run.spawn_id)
    assert row is not None and row.terminal is not None
    assert row.terminal.error == "connection reset while writing prompt"
    assessment = next(
        item for item in claude_scenario.lifecycle(run) if item["event"] == "retry_assessed"
    )
    assert assessment["failure_disposition"] == "transient"
    assert assessment["turn_submission"] == "unknown"
    assert assessment["replay_safety"] == "unknown"
    assert assessment["retry"] is False


@pytest.mark.parametrize("activity", ["disconnect", "model", "tool", "child"])
@pytest.mark.asyncio
async def test_submitted_turn_is_never_replayed_after_transport_close(
    claude_scenario: _ClaudeScenario,
    activity: str,
) -> None:
    side_effect = claude_scenario.root / "tool-side-effect"
    activity_frame = ""
    if activity == "model":
        activity_frame = (
            'printf \'%s\\n\' \'{"type":"assistant","message":{"role":"assistant",'
            '"content":[{"type":"text","text":"draft"}]}}\'\n'
        )
    elif activity == "tool":
        activity_frame = (
            f"touch {shlex.quote(str(side_effect))}\n"
            'printf \'%s\\n\' \'{"type":"assistant","message":{"role":"assistant",'
            '"content":[{"type":"tool_use","id":"tool-1",'
            '"name":"Write","input":{}}]}}\'\n'
        )
    elif activity == "child":
        activity_frame = (
            'printf \'%s\\n\' \'{"type":"assistant","message":{"role":"assistant",'
            '"content":[{"type":"text","text":"launching child"}]}}\'\n'
        )
    log = shlex.quote(str(claude_scenario.invocation_log))
    claude_scenario.install_claude(
        f"printf '%s\\n' \"$*\" >> {log}\n"
        "session_id=\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "--session-id" ]; then shift; session_id=$1; fi\n'
        "  shift\n"
        "done\n"
        "IFS= read -r prompt\n"
        'printf \'{"type":"system","subtype":"init",'
        '"session_id":"%s"}\\n\' "$session_id"\n' + activity_frame + "exit 1\n"
    )
    child_created = False
    parent_id = SpawnId(f"r-claude-close-{activity}")

    def observe_event(_event: object) -> None:
        nonlocal child_created
        if activity != "child" or child_created:
            return
        child_created = True
        spawn_store.start_spawn(
            claude_scenario.runtime_root,
            chat_id="test-chat-child-activity",
            parent_id=str(parent_id),
            model="claude-sonnet-4-5",
            agent="",
            harness=HarnessId.CLAUDE.value,
            kind="streaming",
            prompt="child",
            spawn_id=SpawnId("r-claude-close-child-work"),
            launch_mode="background",
            status="running",
        )
    exit_code, run = await claude_scenario.run(
        f"close-{activity}",
        event_observer=observe_event,
    )

    assert exit_code == 1
    assert len(claude_scenario.invocations()) == 1
    if activity == "tool":
        assert side_effect.exists()
    if activity == "child":
        assert child_created
        child = spawn_store.get_spawn(
            claude_scenario.runtime_root,
            SpawnId("r-claude-close-child-work"),
        )
        assert child is not None and child.parent_id == str(run.spawn_id)
    assessment = next(
        item for item in claude_scenario.lifecycle(run) if item["event"] == "retry_assessed"
    )
    assert assessment["failure_disposition"] == "transient"
    assert assessment["turn_submission"] == "submitted"
    assert assessment["replay_safety"] == "unsafe"
    assert assessment["retry"] is False


@pytest.mark.asyncio
async def test_cancellation_during_retry_backoff_stops_next_attempt(
    claude_scenario: _ClaudeScenario,
) -> None:
    claude_scenario.install_claude("exit 99\n")
    starts = 0

    async def transient_before_launch(_connection: object, _config: object, _spec: object) -> None:
        nonlocal starts
        starts += 1
        raise RetryableConnectionStartupError("temporary launcher failure")

    claude_scenario.monkeypatch.setattr(
        claude_connection_module.ClaudeConnection,
        "_start_subprocess",
        transient_before_launch,
    )
    task = asyncio.create_task(
        claude_scenario.run(
            "cancel-backoff",
            retry=RetryPolicy(max_attempts=2, backoff_secs=5.0),
        )
    )
    attempt_one = claude_scenario.runtime_root / "spawns" / "r-claude-cancel-backoff" / "attempt-1"
    for _ in range(100):
        if attempt_one.is_dir():
            break
        await asyncio.sleep(0.02)
    assert attempt_one.is_dir()
    spawn_store.record_cancel_intent(
        claude_scenario.runtime_root,
        SpawnId("r-claude-cancel-backoff"),
        exit_code=130,
        error="cancelled",
    )

    (exit_code, run) = await asyncio.wait_for(task, timeout=5.0)
    row = spawn_store.get_spawn(claude_scenario.runtime_root, run.spawn_id)
    assert exit_code == 130
    assert starts == 1
    assert row is not None and row.terminal is not None
    assert row.terminal.error == "cancelled"


@pytest.mark.asyncio
async def test_retry_setup_failure_preserves_causal_attempt_error(
    claude_scenario: _ClaudeScenario,
) -> None:
    claude_scenario.install_claude("exit 99\n")

    async def transient_before_launch(_connection: object, _config: object, _spec: object) -> None:
        raise RetryableConnectionStartupError("causal temporary launcher failure")

    def fail_rearm(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("retry plumbing exploded")

    claude_scenario.monkeypatch.setattr(
        claude_connection_module.ClaudeConnection,
        "_start_subprocess",
        transient_before_launch,
    )
    claude_scenario.monkeypatch.setattr(native_run_module.NativeRun, "rearm", fail_rearm)

    exit_code, run = await claude_scenario.run("retry-setup-cause")

    row = spawn_store.get_spawn(claude_scenario.runtime_root, run.spawn_id)
    assert exit_code == 2
    assert row is not None and row.terminal is not None
    assert row.terminal.error == "causal temporary launcher failure"
    setup_failure = next(
        item for item in claude_scenario.lifecycle(run) if item["event"] == "retry_setup_failed"
    )
    assert setup_failure["exception"] == "retry plumbing exploded"
