"""Run exit identity is distinct from immutable entry ownership."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from meridian.lib.harness.pi_boundary import read_boundary
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.launch.process.runner import run_harness_process
from meridian.lib.ops.session_target import resolve_session_log_target
from meridian.lib.state import session_store, spawn_store
from tests.integration.launch.test_pi_identity_launch import (
    context,
    install_shim,
    pi_runtime,  # noqa: F401
)
from tests.support.process_race import run_spawn_race_or_skip


@pytest.mark.parametrize("shape", ["quit", "switch", "restart", "other", "missing",
                                   "truncated", "nonce", "pid", "oversize"])
def test_reader_final_quit_only(tmp_path: Path, shape: str) -> None:
    a = {"session_id": "entry", "session_file": "/store/1_entry.jsonl"}
    b = {"session_id": "exit", "session_file": "/store/2_exit.jsonl"}
    record = {
        "v": 2, "launch_nonce": "nonce", "pid": 100, "revision": 3,
        "initial": a, "current": b if shape == "switch" else a,
        "quit": b if shape == "switch" else a, "invalid_reason": None,
        "last_event": {"type": "session_shutdown", "reason": "quit"},
    }
    if shape == "restart":
        record.update(quit=None, last_event={"type": "session_start", "reason": "new"})
    if shape == "other":
        record["last_event"] = {"type": "session_shutdown", "reason": "resume"}
    if shape == "nonce":
        record["launch_nonce"] = "wrong"
    if shape == "pid":
        record["pid"] = 101
    path = tmp_path / "boundary.json"
    if shape != "missing":
        path.write_text("{" if shape == "truncated" else
                        " " * 16385 if shape == "oversize" else json.dumps(record))
    result = read_boundary(path, nonce="nonce", pid=100)
    assert (result.exit is not None) == (shape in {"quit", "switch"})
    if shape == "switch":
        assert result.entry_observed is not None and result.entry_observed.session_id == "entry"
        assert result.exit is not None and result.exit.session_id == "exit"


@pytest.mark.parametrize("shape", ["quit", "restart"])
def test_reader_consumes_built_bundle_record(tmp_path: Path, shape: str) -> None:
    runtime = Path(__file__).resolve().parents[3] / "src/meridian/pi_runtime"
    fixture = runtime / "extensions/session-boundary/lifecycle.fixture.mjs"
    record = tmp_path / "boundary.json"
    with subprocess.Popen(
        ["node", str(fixture), shape],
        env={
            **os.environ,
            "_MERIDIAN_PI_SESSION_BOUNDARY_PATH": str(record),
            "_MERIDIAN_PI_SESSION_BOUNDARY_NONCE": "reader-test-nonce",
        },
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ) as child:
        try:
            stdout, stderr = child.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate(timeout=5)
            raise
    assert child.returncode == 0, stderr.decode()
    assert stdout == stderr == b""
    boundary = read_boundary(record, nonce="reader-test-nonce", pid=child.pid)
    assert boundary.entry_observed is not None
    assert boundary.entry_observed.native_store == "/native-store"
    assert boundary.entry_observed.session_id == "native-entry"
    if shape == "quit":
        assert boundary.exit is not None
        assert boundary.exit.native_store == "/native-store"
        assert boundary.exit.session_id == "native-exit"
    else:
        assert boundary.exit is None


def install_boundary_shim(root: Path, shape: str) -> None:
    install_shim(root)
    shim = root.parent / "fake-bin" / "pi"
    initial = "wrong-entry" if shape == "mismatch" else "$id"
    exit_id = "$id" if shape == "same" else "switched-id"
    event_type = "session_start" if shape == "restart" else "session_shutdown"
    reason = "new" if shape == "restart" else "quit"
    publication = (
        f'exit_id="{exit_id}"\n'
        '[ "$exit_id" = "$id" ] || printf \'{"type":"session","id":"%s"}\\n\' "$exit_id" '
        '> "$store/2_$exit_id.jsonl"\n'
        'cat > "$_MERIDIAN_PI_SESSION_BOUNDARY_PATH" <<EOF\n'
        '{"v":2,"launch_nonce":"$_MERIDIAN_PI_SESSION_BOUNDARY_NONCE","pid":$$,"revision":5,'
        f'"initial":{{"session_id":"{initial}","session_file":"$store/1_{initial}.jsonl"}},'
        '"current":{"session_id":"$exit_id","session_file":"$store/2_$exit_id.jsonl"},'
        f'"last_event":{{"type":"{event_type}","reason":"{reason}"}},'
        '"quit":{"session_id":"$exit_id","session_file":"$store/2_$exit_id.jsonl"},'
        '"invalid_reason":null}\nEOF\n'
    )
    if shape == "truncated":
        publication = 'printf "{" > "$_MERIDIAN_PI_SESSION_BOUNDARY_PATH"\n'
    shim.write_text(shim.read_text().replace(
        'if [ "$rpc" != "rpc" ]; then exit 0; fi',
        publication + 'if [ "$rpc" != "rpc" ]; then exit 0; fi',
    ))


@pytest.mark.parametrize("shape", ["same", "switch", "restart", "mismatch", "truncated"])
def test_primary_post_exit_boundary(pi_runtime: Path, shape: str) -> None:  # noqa: F811
    root = pi_runtime
    install_boundary_shim(root, shape)
    outcome = run_harness_process(context(root), HarnessRegistry.with_defaults())
    assert outcome.primary_spawn_id is not None and outcome.chat_id is not None
    runtime = root / ".meridian"
    row = spawn_store.get_spawn(runtime, outcome.primary_spawn_id)
    assert row is not None
    entry = session_store.get_session_record(runtime, outcome.chat_id)
    assert entry is not None
    assert row.entry_chat_id == outcome.chat_id
    assert entry.harness_session_id != "switched-id"
    assert outcome.exit_code == (1 if shape == "mismatch" else 0)
    expected = ("verified" if shape in {"same", "switch"} else
                "mismatch" if shape == "mismatch" else "unresolved")
    assert row.exit_identity == expected
    meta = json.loads((runtime / "spawns" / row.id / "primary_meta.json").read_text())
    assert meta["exit_identity"] == expected
    if shape == "mismatch":
        assert_entry_mismatch(runtime, row.id, entry)
    if shape == "same":
        assert row.exit_chat_id == row.entry_chat_id
    if shape == "switch":
        assert row.exit_chat_id != row.entry_chat_id
        exit_chat = session_store.get_session_record(runtime, row.exit_chat_id)
        assert exit_chat is not None and exit_chat.harness_session_id == "switched-id"
    if shape != "mismatch":
        target = resolve_session_log_target(
            ref=row.id, file_path=None, project_root=root, runtime_root=runtime,
        )
        expected_id = "switched-id" if shape == "switch" else entry.harness_session_id
        assert target.session_id == expected_id
        assert ("entry-based view" in target.source) == (shape in {"restart", "truncated"})


def assert_entry_mismatch(runtime: Path, spawn_id: str, entry: session_store.SessionRecord) -> None:
    row = spawn_store.get_spawn(runtime, spawn_id)
    assert row is not None and row.status == "failed"
    assert row.terminal is not None and row.terminal.error == "entry_mismatch"
    assert row.exit_identity == "mismatch" and row.exit_chat_id is None
    facts = [json.loads(line) for line in (
        runtime / "spawns" / spawn_id / "runner-lifecycle.jsonl"
    ).read_text().splitlines()]
    mismatch = [fact for fact in facts if fact["event"] == "entry_mismatch"]
    assert len(mismatch) == 1
    assert mismatch[0]["expected"] == f"({entry.native_store}, {entry.harness_session_id})"
    assert mismatch[0]["observed"] == f"({entry.native_store}, wrong-entry)"
    assert all(record.harness_session_id not in {"wrong-entry", "switched-id"}
               for record in session_store.list_all_session_records(runtime))


def test_concurrent_exits_converge_on_one_stopped_chat(pi_runtime: Path) -> None:  # noqa: F811
    root = pi_runtime
    install_shim(root)
    outcome = run_harness_process(context(root), HarnessRegistry.with_defaults())
    assert outcome.chat_id is not None
    args = (root / ".meridian", outcome.chat_id, "pi", "/exit-store", "exit-id")
    ids = run_spawn_race_or_skip(session_store.get_or_create_exit_chat, [args, args])
    assert ids[0] == ids[1] != outcome.chat_id
    record = session_store.get_session_record(root / ".meridian", ids[0])
    assert record is not None and record.stopped_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["switch", "mismatch"])
async def test_rpc_post_attempt_boundary(pi_runtime: Path, shape: str) -> None:  # noqa: F811
    import asyncio
    from dataclasses import replace
    from types import MappingProxyType

    from meridian.lib.core.domain import Spawn
    from meridian.lib.core.types import ModelId, SpawnId
    from meridian.lib.launch.session_scope import session_scope
    from meridian.lib.launch.streaming_runner import execute_with_streaming
    from meridian.lib.launch.types import PrimarySessionMetadata
    from meridian.lib.state.artifact_store import LocalStore

    root = pi_runtime
    install_boundary_shim(root, shape)
    ctx = context(root, primary=False)
    run = Spawn(spawn_id=SpawnId("p42"), prompt="hello", model=ModelId("pi-test"), status="queued")
    spawn_store.start_spawn(
        ctx.runtime_root, spawn_id=run.spawn_id, chat_id="", model="pi-test", agent="",
        harness="pi", kind="streaming", prompt="hello", status="queued",
    )
    env = dict(ctx.binding.environment.final_env)
    prepared = ctx.harness.prepare_prelaunch(
        runtime_root=ctx.runtime_root, spawn_id=SpawnId("p42"), session=ctx.request.session,
        child_cwd=root, child_env=env, resolved_harness_session_id="",
    )
    env.update(prepared.env_overrides)
    ctx = replace(ctx, binding=replace(ctx.binding, environment=replace(
        ctx.binding.environment, final_env=MappingProxyType(env),
    )))
    with session_scope(
        runtime_root=ctx.runtime_root,
        metadata=PrimarySessionMetadata(
            harness="pi", model="pi-test", agent="", agent_path="", skills=(), skill_paths=(),
        ),
        request=ctx.request.session, harness_session_id="", spawn_id="p42",
        startup_attempt_id="boundary-test",
    ) as managed:
        code = await asyncio.wait_for(execute_with_streaming(
            run, request=ctx.request, launch_context=ctx, project_root=root,
            runtime_root=ctx.runtime_root,
            artifacts=LocalStore(root_dir=ctx.runtime_root / "artifacts"),
            session_attempt=managed.attempt,
        ), 20)
        assert code == (1 if shape == "mismatch" else 0)
        entry = session_store.get_session_record(ctx.runtime_root, managed.chat_id)
        assert entry is not None and entry.harness_session_id not in {"wrong-entry", "switched-id"}
        if shape == "mismatch":
            assert_entry_mismatch(ctx.runtime_root, "p42", entry)
            return
        row = spawn_store.get_spawn(ctx.runtime_root, "p42")
        assert row is not None and row.exit_identity == "verified"
        assert row.entry_chat_id == managed.chat_id and row.exit_chat_id != managed.chat_id
