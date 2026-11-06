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
from meridian.lib.state.spawn_store import start_spawn
from tests.support.launch_paths import launch_paths

HELP = (
    "--mode rpc --model --append-system-prompt --session --session-id --fork "
    "--session-dir --no-extensions -e --extension"
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
        + startup
        + "\nfor line in sys.stdin:\n"
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
        harness_id=HarnessId.PI,
        prompt=prompt,
        control_root=root,
        launch=launch_paths(runtime_root, spawn_id),
        pi_session_role="spawned",
        child_env={**os.environ, "MERIDIAN_PI_BINARY": str(shim)},
    )
    spec = ResolvedLaunchSpec(
        harness=HarnessId.PI,
        prompt=prompt,
    )
    try:
        await connection.start(config, spec)
        yield connection
    finally:
        await connection.stop()


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
async def test_large_prompt_and_startup_frame_do_not_block_each_other(tmp_path: Path) -> None:
    startup = (
        "print(json.dumps({'type':'session','id':'startup','data':'x'*(512*1024)}),flush=True)"
    )
    body = (
        "print(json.dumps({'type':'response','command':'prompt',"
        "'id':command['id'],'success':True}),flush=True)"
    )
    async with rpc_process(
        tmp_path,
        body,
        startup=startup,
        prompt="x" * (512 * 1024),
        timing=PiRpcTimingPolicy(
            prompt_ack_timeout_seconds=1, abort_grace_seconds=0.05, kill_grace_seconds=0.05
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
