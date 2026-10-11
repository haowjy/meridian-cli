"""Real stdio regressions for Pi reception independent of event consumption."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.connections.base import ConnectionConfig
from meridian.lib.harness.connections.pi_rpc import PiRpcConnection, PiRpcTimingPolicy
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.state.spawn_store import start_spawn

HELP = (
    "--mode rpc --model --append-system-prompt --session --session-id --fork "
    "--session-dir --no-extensions --no-skills --no-context-files --no-prompt-templates "
    "-e --extension"
)


@asynccontextmanager
async def rpc_process(
    root: Path,
    body: str,
    *,
    timing: PiRpcTimingPolicy | None = None,
    prompt: str = "FIRST",
    startup: str = "",
) -> AsyncIterator[PiRpcConnection]:
    runtime_root = root / "runtime"
    spawn_id = SpawnId("rpc-reception")
    start_spawn(
        runtime_root,
        spawn_id=spawn_id,
        chat_id="chat-1",
        model="test",
        agent="tester",
        harness="pi",
        prompt="FIRST",
        status="running",
    )
    shim = root / "pi"
    shim.write_text(
        f"#!{sys.executable}\nimport json,os,sys,time\n"
        "if '--version' in sys.argv: print('1.1.0'); raise SystemExit\n"
        f"if '--help' in sys.argv: print({HELP!r}); raise SystemExit\n"
        + startup + "\nfor line in sys.stdin:\n"
        " command=json.loads(line)\n"
        " if command['type']=='abort': raise SystemExit\n"
        " if command['type']!='prompt': continue\n"
        " if command['message']=='FIRST':\n"
        "  print(json.dumps({'type':'response','command':'prompt',"
        "'id':command['id'],'success':True}),flush=True)\n"
        "  continue\n" + "\n".join(" " + line for line in body.splitlines()) + "\n",
    )
    shim.chmod(0o755)
    connection = PiRpcConnection(
        timing=timing or PiRpcTimingPolicy(abort_grace_seconds=0.05, kill_grace_seconds=0.05)
    )
    config = ConnectionConfig(
        spawn_id=spawn_id,
        harness_id=HarnessId.PI,
        prompt=prompt,
        control_root=root,
        runtime_root=runtime_root,
        pi_session_role="spawned",
        child_env={**os.environ, "MERIDIAN_PI_BINARY": str(shim)},
    )
    spec = ResolvedLaunchSpec(
        harness=HarnessId.PI,
        prompt=prompt,
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )
    try:
        await connection.start(config, spec)
        yield connection
    finally:
        await connection.stop()


@pytest.mark.asyncio
async def test_large_prompt_and_startup_frame_do_not_block_each_other(tmp_path: Path) -> None:
    startup = (
        "print(json.dumps({'type':'session','id':'startup','data':'x'*(512*1024)}),flush=True)"
    )
    body = (
        "print(json.dumps({'type':'response','command':'prompt',"
        "'id':command['id'],'success':True}),flush=True)"
    )
    async with rpc_process(
        tmp_path, body, startup=startup, prompt="x" * (512 * 1024),
        timing=PiRpcTimingPolicy(
            prompt_ack_timeout_seconds=1, abort_grace_seconds=.05, kill_grace_seconds=.05
        ),
    ) as connection:
        events = connection.events()
        try:
            while (event := await asyncio.wait_for(anext(events), 1)).event_type != "response":
                pass
            assert event.payload["success"] is True
            assert connection.session_id == "startup"
        finally:
            await events.aclose()


@pytest.mark.asyncio
async def test_prompt_ack_is_received_without_advancing_event_iterator(tmp_path: Path) -> None:
    body = (
        "print(json.dumps({'type':'agent_start'}),flush=True)\n"
        "print(json.dumps({'type':'response','command':'prompt','id':command['id'],'success':True}),flush=True)"
    )
    async with rpc_process(tmp_path, body) as connection:
        # No anext is outstanding. A completion consumer can await this nudge.
        await asyncio.wait_for(connection.send_user_message("nudge"), 0.5)
        events = connection.events()
        seen = []
        try:
            while len([event for event in seen if event.event_type == "response"]) < 2:
                seen.append(await asyncio.wait_for(anext(events), 0.5))
            responses = [event for event in seen if event.event_type == "response"]
            assert responses[0].payload.get("meridian_control_action") is None
            assert responses[1].payload["meridian_control_action"] == "inject"
        finally:
            await events.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["message_end", "agent_end"])
async def test_large_frame_preserves_tool_output_and_next_response(
    tmp_path: Path, event_type: str
) -> None:
    aggregate = event_type == "agent_end"
    size = (13 if aggregate else 11) * 1024 * 1024
    content = (
        f"{{'type':'image','mimeType':'image/png','data':'x'*{size}}}"
        if aggregate
        else f"{{'type':'text','text':'x'*{size}}}"
    )
    body = (
        "message={'role':'toolResult',"
        "'toolCallId':'large-tool','toolName':'bash',"
        f"'content':[{content}]}}\n"
        f"print(json.dumps({{'type':{event_type!r},"
        + ("'messages':[message]" if aggregate else "'message':message")
        + "}),flush=True)\n"
        "print(json.dumps({'type':'response','command':'prompt','id':command['id'],'success':True}),flush=True)"
    )
    async with rpc_process(tmp_path, body) as connection:
        sent = asyncio.create_task(connection.send_user_message("large"))
        events = connection.events()
        try:
            while True:
                event = await asyncio.wait_for(anext(events), 2)
                if event.event_type == event_type:
                    break
                assert event.event_type != "meridian/error/connectionClosed", event.payload
            message = event.payload["messages"][0] if aggregate else event.payload["message"]
            assert len(message["content"][0]["data" if aggregate else "text"]) == size
            assert message["toolName"] == "bash"
            await asyncio.wait_for(sent, 2)
        finally:
            sent.cancel()
            await asyncio.gather(sent, return_exceptions=True)
            await events.aclose()


@pytest.mark.asyncio
async def test_missing_ack_has_deadline_and_late_ack_is_still_control_evidence(
    tmp_path: Path,
) -> None:
    body = (
        "time.sleep(0.15)\n"
        "print(json.dumps({'type':'response','command':'prompt','id':command['id'],'success':False,'error':'late'}),flush=True)"
    )
    timing = PiRpcTimingPolicy(
        prompt_ack_timeout_seconds=0.05, abort_grace_seconds=0.05, kill_grace_seconds=0.05
    )
    async with rpc_process(tmp_path, body, timing=timing) as connection:
        with pytest.raises(RuntimeError, match=r"pi_rpc_prompt_ack_timeout.*delivery uncertain"):
            await asyncio.wait_for(connection.send_user_message("nudge"), 0.5)
        events = connection.events()
        try:
            while True:
                event = await asyncio.wait_for(anext(events), 0.5)
                if event.payload.get("error") == "late":
                    assert event.payload["meridian_control_action"] == "inject"
                    break
        finally:
            await events.aclose()


@pytest.mark.asyncio
async def test_stdout_closure_with_live_child_fails_and_reaps_child(tmp_path: Path) -> None:
    body = "os.close(1)\ntime.sleep(60)"
    timing = PiRpcTimingPolicy(
        eof_exit_timeout_seconds=0.05, abort_grace_seconds=0.05, kill_grace_seconds=0.05
    )
    async with rpc_process(tmp_path, body, timing=timing) as connection:
        sent = asyncio.create_task(connection.send_user_message("close"))
        events = connection.events()
        try:
            while True:
                event = await asyncio.wait_for(anext(events), 1)
                if event.event_type == "meridian/error/connectionClosed":
                    assert "pi_rpc_stdout_closed_while_process_alive" in event.payload["message"]
                    break
            with pytest.raises(Exception, match="closed before prompt acknowledgement"):
                await asyncio.wait_for(sent, 1)
            assert not connection.health()
        finally:
            sent.cancel()
            await asyncio.gather(sent, return_exceptions=True)
            await events.aclose()


@pytest.mark.asyncio
async def test_live_budget_stops_on_sum_of_pi_message_costs(tmp_path: Path) -> None:
    from meridian.lib.bootstrap.services import build_spawn_lifecycle_service_from_roots
    from meridian.lib.core.domain import Spawn
    from meridian.lib.harness.extractors.pi import PiHarnessExtractor
    from meridian.lib.launch.streaming_runner import _run_streaming_attempt
    from meridian.lib.safety.budget import Budget, LiveBudgetTracker
    from meridian.lib.streaming.spawn_manager import SpawnManager
    from tests.support.pi import NoopControlServer

    body = (
        "print(json.dumps({'type':'tool_execution_end','toolName':'invoice',"
        "'result':{'details':{'cost':100}}}),flush=True)\n"
        "for i in range(2):\n"
        " print(json.dumps({'type':'message_end','message':{'role':'assistant','content':[],"
        "'usage':{'input':1,'output':1,'cacheRead':0,'cacheWrite':0,'cost':{'total':0.9}}}}),flush=True)\n"
        "print(json.dumps({'type':'response','command':'prompt','id':command['id'],'success':True}),flush=True)"
    )
    async with rpc_process(tmp_path, body) as connection:
        runtime_root = tmp_path / "runtime"
        spawn_id = SpawnId("rpc-reception")
        run = Spawn(spawn_id=spawn_id, prompt="FIRST", model="test", status="running")

        async def started(config, spec):
            return connection

        manager = SpawnManager(
            runtime_root,
            tmp_path,
            start_connection=started,
            control_server_factory=lambda *_args: NoopControlServer(),
        )
        fold = PiHarnessExtractor().create_fold()
        sent = None

        def on_running(_connection):
            nonlocal sent
            sent = asyncio.create_task(connection.send_user_message("cost"))

        try:
            attempt = await asyncio.wait_for(
                _run_streaming_attempt(
                    run=run,
                    runtime_root=runtime_root,
                    launch_mode="foreground",
                    log_dir=runtime_root / "spawns" / str(spawn_id),
                    manager=manager,
                    config=ConnectionConfig(
                        spawn_id=spawn_id,
                        harness_id=HarnessId.PI,
                        prompt="FIRST",
                        control_root=tmp_path,
                        runtime_root=runtime_root,
                        child_env={},
                    ),
                    run_spec=ResolvedLaunchSpec(
                        harness=HarnessId.PI,
                        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
                    ),
                    budget_tracker=LiveBudgetTracker(budget=Budget(per_run_usd=1.5)),
                    signal_event=asyncio.Event(),
                    received_signal=[None],
                    timeout_seconds=None,
                    event_observer=None,
                    stream_stdout_to_terminal=False,
                    lifecycle_service=build_spawn_lifecycle_service_from_roots(
                        tmp_path, runtime_root
                    ),
                    on_running=on_running,
                    event_hook=fold,
                    attempt_facts=fold.facts,
                ),
                3,
            )
            assert attempt.start_error is None
            assert attempt.budget_breach is not None
            assert attempt.budget_breach.observed_usd == pytest.approx(1.8)
            assert attempt.drain_error == "budget_exceeded"
            assert not connection.health()
        finally:
            if sent is not None:
                await asyncio.gather(sent, return_exceptions=True)
            await manager.shutdown()
