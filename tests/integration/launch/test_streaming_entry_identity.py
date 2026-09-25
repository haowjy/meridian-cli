"""Owned startup identity must confirm entry, not silently substitute another chat."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from meridian.lib.core.domain import Spawn, SpawnStatus
from meridian.lib.core.types import HarnessId, ModelId, SpawnId, TransportId
from meridian.lib.harness.connections.base import ConnectionConfig, RawHarnessEvent
from meridian.lib.harness.launch_spec import ResolvedLaunchSpec
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.launch.request import SessionRequest, SpawnRequest
from meridian.lib.launch.session_scope import session_scope
from meridian.lib.launch.types import PrimarySessionMetadata
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.artifact_store import LocalStore
from meridian.lib.state.paths import resolve_project_runtime_root_for_write, resolve_spawn_log_dir
from meridian.lib.streaming import spawn_manager as spawn_manager_module
from tests.integration.launch.streaming_runner_support import (
    _execute_with_context,
    _FakeControlSocketServer,
)
from tests.integration.launch.test_streaming_runner_seed import _ClaudeSeedPersistenceConnection


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "resume", "fork"])
@pytest.mark.parametrize("delivery", ["property", "callback", "duplicate"])
@pytest.mark.parametrize("mismatch", [False, True])
async def test_streaming_initial_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    delivery: str,
    mismatch: bool,
) -> None:
    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
    expected_id = ""
    observed_id = ""

    class Connection(_ClaudeSeedPersistenceConnection):
        @property
        def session_id(self) -> str:
            return observed_id

        async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
            nonlocal expected_id, observed_id
            self._spawn_id = config.spawn_id
            self._project_root = config.control_root
            self.state = "connected"
            assert spec.native_identity_plan is not None
            expected_id = spec.native_identity_plan.harness_session_id or ""
            observed_id = (
                "source-native"
                if operation == "fork" and mismatch
                else "unexpected-native"
                if mismatch
                else "fork-target"
                if operation == "fork"
                else expected_id
            )
            if delivery != "property":
                assert config.session_id_observer is not None
                config.session_id_observer(observed_id)
                if delivery == "duplicate":
                    config.session_id_observer(observed_id)

    def connection_class(
        _harness_id: HarnessId,
        _transport_id: TransportId = TransportId.STREAMING,
    ) -> type[Connection]:
        return Connection

    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _FakeControlSocketServer)
    monkeypatch.setattr("meridian.lib.harness.connections.get_connection_class", connection_class)
    request = SpawnRequest(
        model="claude-sonnet-4-6",
        harness="claude",
        prompt="hello",
        session=SessionRequest(
            requested_harness_session_id="source-native" if operation != "create" else None,
            continue_fork=operation == "fork",
        ),
    )
    run = Spawn(
        spawn_id=SpawnId("p42"),
        prompt="hello",
        model=ModelId(request.model or ""),
        status=SpawnStatus.QUEUED,
    )
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=run.spawn_id,
        chat_id="",
        model=str(run.model),
        agent="",
        harness="claude",
        kind="streaming",
        prompt=run.prompt,
        status=SpawnStatus.QUEUED,
    )
    with session_scope(
        runtime_root=runtime_root,
        metadata=PrimarySessionMetadata(
            harness="claude",
            model=str(run.model),
            agent="",
            agent_path="",
            skills=(),
            skill_paths=(),
        ),
        request=request.session,
        harness_session_id="source-native" if operation == "resume" else "",
        spawn_id=str(run.spawn_id),
        startup_attempt_id="attempt-entry",
    ) as managed:
        exit_code = await asyncio.wait_for(
            _execute_with_context(
                run,
                request=request,
                project_root=tmp_path,
                runtime_root=runtime_root,
                artifacts=LocalStore(root_dir=runtime_root / "artifacts"),
                registry=HarnessRegistry.with_defaults(),
                session_attempt=managed.attempt,
            ),
            timeout=15,
        )
        record = session_store.get_session_record(runtime_root, managed.chat_id)
        assert record is not None
        assert record.harness_session_id == ((expected_id or None) if mismatch else observed_id)

    row = spawn_store.get_spawn(runtime_root, run.spawn_id)
    assert row is not None
    events = [
        json.loads(line) for line in (runtime_root / "sessions.jsonl").read_text().splitlines()
    ]
    starts = [event for event in events if event.get("kind") == "invocation_started"]
    if mismatch:
        assert exit_code != 0
        assert row.status == "failed"
        assert row.terminal is not None
        assert row.terminal.error == "entry_mismatch"
        assert starts == []
        lifecycle = resolve_spawn_log_dir(tmp_path, run.spawn_id, runtime_root=runtime_root)
        facts = [
            json.loads(line)
            for line in (lifecycle / "runner-lifecycle.jsonl").read_text().splitlines()
        ]
        assert any(fact["event"] == "entry_mismatch" for fact in facts)
        assert not any(event.get("harness_session_id") == observed_id for event in events)
    else:
        assert exit_code == 0
        assert row.status == "succeeded"
        assert len(starts) == 1
        assert starts[0]["harness_session_id"] == observed_id


@pytest.mark.asyncio
async def test_later_switch_does_not_rebind_or_fail_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = resolve_project_runtime_root_for_write(tmp_path)
    expected_id = ""

    class Connection(_ClaudeSeedPersistenceConnection):
        _config: ConnectionConfig
        _native_id: str

        @property
        def session_id(self) -> str:
            return self._native_id

        async def start(self, config: ConnectionConfig, spec: ResolvedLaunchSpec) -> None:
            nonlocal expected_id
            await super().start(config, spec)
            assert spec.native_identity_plan is not None
            expected_id = spec.native_identity_plan.harness_session_id or ""
            self._native_id = expected_id
            self._config = config
            assert config.session_id_observer is not None
            config.session_id_observer(expected_id)

        async def events(self):  # type: ignore[no-untyped-def]
            self._native_id = "switched-native"
            assert self._config.session_id_observer is not None
            self._config.session_id_observer(self._native_id)
            yield RawHarnessEvent(
                event_type="result",
                harness_id="claude",
                payload={"type": "result", "result": "done"},
            )

    def connection_class(
        _harness_id: HarnessId,
        _transport_id: TransportId = TransportId.STREAMING,
    ) -> type[Connection]:
        return Connection

    monkeypatch.setattr(spawn_manager_module, "ControlSocketServer", _FakeControlSocketServer)
    monkeypatch.setattr("meridian.lib.harness.connections.get_connection_class", connection_class)
    request = SpawnRequest(model="claude-sonnet-4-6", harness="claude", prompt="hello")
    run = Spawn(
        spawn_id=SpawnId("p42"),
        prompt="hello",
        model=ModelId(request.model or ""),
        status=SpawnStatus.QUEUED,
    )
    spawn_store.start_spawn(
        runtime_root,
        spawn_id=run.spawn_id,
        chat_id="",
        model=str(run.model),
        agent="",
        harness="claude",
        kind="streaming",
        prompt=run.prompt,
        status=SpawnStatus.QUEUED,
    )
    with session_scope(
        runtime_root=runtime_root,
        metadata=PrimarySessionMetadata(
            harness="claude",
            model=str(run.model),
            agent="",
            agent_path="",
            skills=(),
            skill_paths=(),
        ),
        request=request.session,
        harness_session_id="",
        spawn_id=str(run.spawn_id),
        startup_attempt_id="attempt-entry",
    ) as managed:
        exit_code = await asyncio.wait_for(
            _execute_with_context(
                run,
                request=request,
                project_root=tmp_path,
                runtime_root=runtime_root,
                artifacts=LocalStore(root_dir=runtime_root / "artifacts"),
                registry=HarnessRegistry.with_defaults(),
                session_attempt=managed.attempt,
            ),
            timeout=15,
        )
        record = session_store.get_session_record(runtime_root, managed.chat_id)
        assert record is not None
        assert record.harness_session_id == expected_id
    assert exit_code == 0
