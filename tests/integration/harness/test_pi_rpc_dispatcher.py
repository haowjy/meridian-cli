"""Single-reader response correlation and deferred input over real subprocess stdio."""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
from pathlib import Path

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import ConnectionConfig, ConnectionNotReady
from meridian.lib.harness.connections.pi_rpc import PiRpcConnection, PiRpcTimingPolicy
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.state import spawn_store
from tests.support.async_determinism import wait_until


@pytest.fixture
async def peer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "extensions"
    for name in ("managed-bash", "meridian-spawn-watch"):
        (source / name).mkdir(parents=True)
        (source / name / "index.js").write_text("export default {}\n")
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_SOURCE_ROOT", str(source))
    monkeypatch.setenv("MERIDIAN_PI_EXTENSION_TARGET_ROOT", str(tmp_path / "projected"))
    script = Path(__file__).parents[2] / "fixtures/pi/rpc_dispatcher.py"
    executable = tmp_path / "pi"
    executable.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n'
    )
    executable.chmod(0o755)
    connections = []
    spawn_store.start_spawn(
        tmp_path,
        spawn_id=SpawnId("p-test"),
        chat_id="p-test",
        model="test-model",
        agent="test-agent",
        harness="pi",
        prompt="synthetic",
    )

    async def launch(scenario="normal", *, prompt="FIRST", deferred=True):
        connection = PiRpcConnection(
            timing=PiRpcTimingPolicy(
                command_timeout_seconds=0.5,
                first_event_timeout_seconds=0.5,
                abort_grace_seconds=0.2,
                kill_grace_seconds=0.2,
            )
        )
        connections.append(connection)
        (tmp_path / "spawns/p-test").mkdir(parents=True, exist_ok=True)
        config = ConnectionConfig(
            spawn_id=SpawnId("p-test"),
            harness_id=HarnessId.PI,
            prompt=prompt,
            control_root=tmp_path,
            runtime_root=tmp_path,
            pi_session_role="spawned",
            child_env={
                "MERIDIAN_PI_BINARY": str(executable),
                "SCENARIO": scenario,
                "HOME": str(tmp_path),
                "PI_CODING_AGENT_DIR": str(tmp_path / "agent"),
            },
        )
        spec = ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt=prompt,
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        )
        if deferred:
            await connection.initialize_without_input(config, spec)
        else:
            await connection.start(config, spec)
        return connection

    yield launch
    for connection in connections:
        await asyncio.wait_for(connection.stop(), 5)


def commands(root: Path):
    path = root / "inbound.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.asyncio
async def test_deferred_delivery_owner_barrier_and_consumer_independent_ack(peer, tmp_path):
    connection = await peer()
    assert commands(tmp_path) == []
    for send in (connection.send_user_message, connection.send_steer):
        with pytest.raises(ConnectionNotReady, match="not been released"):
            await send("BLOCKED")
    assert (await connection.get_state())["sessionId"] == "A"
    assert (await connection.get_state())["sessionId"] == "A"  # duplicate/stale replies rejected
    assert [c["type"] for c in commands(tmp_path)] == ["get_state", "get_state"]
    # Stand-in for the future owner's durable barrier. The connection does not admit it.
    (tmp_path / "owner-barrier").write_text("accepted")
    await connection.deliver_initial_prompt()
    await asyncio.wait_for(connection.send_user_message("SECOND"), 2)
    await connection.send_steer("STEER")
    await connection.send_cancel()
    events = [event async for event in connection.events()]
    assert [c["type"] for c in commands(tmp_path)] == [
        "get_state",
        "get_state",
        "prompt",
        "prompt",
        "steer",
        "abort",
    ]
    assert any(e.payload.get("meridian_control_action") == "inject" for e in events)
    assert any(e.event_type == "extension_error" for e in events)
    with pytest.raises(RuntimeError, match="already consumed"):
        await anext(connection.events())


@pytest.mark.asyncio
async def test_event_consumer_close_does_not_stop_dispatcher(peer):
    connection = await peer(prompt="")
    iterator = connection.events()
    await anext(iterator)
    await iterator.aclose()
    assert (await connection.get_state())["sessionId"] == "A"
    await connection.deliver_initial_prompt()
    await asyncio.wait_for(connection.send_user_message("after consumer close"), 2)


@pytest.mark.asyncio
async def test_switch_ack_and_query_hold_gate_through_delivery(peer, tmp_path):
    connection = await peer("switch_barrier")
    switching = asyncio.create_task(connection.switch_session(str(tmp_path / "B.jsonl")))
    await wait_until(lambda: len(commands(tmp_path)) == 1)
    delivery = asyncio.create_task(connection.deliver_initial_prompt())
    (tmp_path / "release-switch").touch()
    await wait_until(lambda: len(commands(tmp_path)) == 2)
    assert [c["type"] for c in commands(tmp_path)] == ["switch_session", "get_state"]
    assert not delivery.done()
    (tmp_path / "release-query").touch()
    assert (await switching)["sessionId"] == "B"
    await delivery
    await connection.send_user_message("ACK")
    assert [c["type"] for c in commands(tmp_path)] == [
        "switch_session",
        "get_state",
        "prompt",
        "prompt",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario, error",
    [
        ("wrong_command", "command mismatch"),
        ("rejected", "rejected"),
        ("truthy", "rejected"),
        ("bad_state", "sessionId/sessionFile"),
        ("parse", "parse error"),
        ("eof", "stdout closed"),
        ("long_line", "stdout closed"),
        ("overflow", "queue overflow"),
    ],
)
async def test_failed_queries_never_deliver_input(peer, tmp_path, scenario, error):
    connection = await peer(scenario)
    with pytest.raises((RuntimeError, ConnectionNotReady), match=error):
        await asyncio.wait_for(connection.get_state(), 3)
    assert [c["type"] for c in commands(tmp_path)] == ["get_state"]
    if scenario == "overflow":
        events = [event async for event in connection.events()]
        assert events[-1].payload["message"] == "Pi RPC event queue overflow."
        assert len(events) <= 257


@pytest.mark.asyncio
async def test_wrong_id_cannot_complete_request(peer):
    connection = await peer("wrong_only")
    with pytest.raises(TimeoutError):
        await connection.get_state()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_late_reply_cannot_complete_successor_request(peer, tmp_path, cancel):
    connection = await peer("late")
    query = asyncio.create_task(connection.get_state())
    await wait_until(lambda: len(commands(tmp_path)) == 1)
    if cancel:
        query.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else TimeoutError):
        await query
    successor = asyncio.create_task(connection.get_state())
    (tmp_path / "release-query").touch()
    assert (await successor)["sessionId"] == "A"
    ids = [c["id"] for c in commands(tmp_path)]
    assert len(ids) == len(set(ids)) == 2


@pytest.mark.asyncio
async def test_cancel_unblocks_ack_without_event_consumer(peer):
    connection = await peer("pending_prompt", prompt="")
    await connection.deliver_initial_prompt()
    injection = asyncio.create_task(connection.send_user_message("WAIT"))
    await asyncio.sleep(0)  # Let injection enter its command wait.
    await connection.send_cancel()
    with pytest.raises(ConnectionNotReady, match="cancelled"):
        await asyncio.wait_for(injection, 2)


@pytest.mark.asyncio
async def test_cancelled_selection_never_reopens_input(peer, tmp_path):
    connection = await peer()
    switching = asyncio.create_task(connection.switch_session(str(tmp_path / "B.jsonl")))
    await wait_until(lambda: len(commands(tmp_path)) == 1)
    switching.cancel()
    with pytest.raises(asyncio.CancelledError):
        await switching
    (tmp_path / "release-switch").touch()
    with pytest.raises(ConnectionNotReady, match="selection did not settle"):
        await connection.deliver_initial_prompt()
    assert all(c["type"] != "prompt" for c in commands(tmp_path))


@pytest.mark.asyncio
async def test_first_event_timeout_is_armed_on_deferred_delivery(peer):
    connection = await peer("silent_prompt")
    assert (await connection.get_state())["sessionId"] == "A"
    await connection.deliver_initial_prompt()
    events = await asyncio.wait_for(_collect(connection), 3)
    assert any(e.payload.get("phase") == "first_pi_event_timeout" for e in events)


async def _collect(connection):
    return [event async for event in connection.events()]


@pytest.mark.asyncio
async def test_native_cancelled_switch_is_queried_before_gate_reopens(peer, tmp_path):
    connection = await peer("cancelled_switch")
    (tmp_path / "release-switch").touch()
    with pytest.raises(RuntimeError, match="switch cancelled"):
        await connection.switch_session(str(tmp_path / "B.jsonl"))
    assert [c["type"] for c in commands(tmp_path)] == ["switch_session", "get_state"]
    assert (await connection.get_state())["sessionId"] == "A"
    await connection.deliver_initial_prompt()
    await connection.send_user_message("ACK")


@pytest.mark.asyncio
async def test_selection_timeout_poison_blocks_queued_input(peer, tmp_path):
    connection = await peer()
    switching = asyncio.create_task(connection.switch_session(str(tmp_path / "B.jsonl")))
    await wait_until(lambda: len(commands(tmp_path)) == 1)
    delivery = asyncio.create_task(connection.deliver_initial_prompt())
    with pytest.raises(TimeoutError):
        await switching
    with pytest.raises(ConnectionNotReady, match="selection did not settle"):
        await delivery
    (tmp_path / "release-switch").touch()
    assert [c["type"] for c in commands(tmp_path)] == ["switch_session"]


@pytest.mark.asyncio
async def test_ack_before_eof_remains_accepted(peer):
    connection = await peer("ack_then_eof", prompt="")
    await connection.deliver_initial_prompt()
    await asyncio.wait_for(connection.send_user_message("LAST"), 2)
    events = [event async for event in connection.events()]
    assert any(e.payload.get("meridian_control_action") == "inject" for e in events)


@pytest.mark.asyncio
async def test_cancelled_initial_write_cannot_redeliver(peer, tmp_path):
    connection = await peer("blocked_input", prompt="x" * (1024 * 1024))
    delivery = asyncio.create_task(connection.deliver_initial_prompt())
    try:
        await wait_until(lambda: (tmp_path / "input-started").exists())
        assert not delivery.done()  # Real pipe backpressure: child read only the first byte.
        delivery.cancel()
        with pytest.raises(asyncio.CancelledError):
            await delivery
    finally:
        (tmp_path / "release-input").touch()
    with pytest.raises(ConnectionNotReady, match="initial delivery did not settle"):
        await connection.deliver_initial_prompt()
    await wait_until(lambda: (tmp_path / "prompt-recorded").exists())
    assert [c["type"] for c in commands(tmp_path)] == ["prompt"]
