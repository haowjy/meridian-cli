"""Run exit identity is distinct from immutable entry ownership."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from meridian.cli.primary_launch import PrimaryLaunchOutput, run_primary_launch
from meridian.lib.harness.pi_boundary import read_boundary
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.launch.process.runner import run_harness_process
from meridian.lib.ops.reference import resolve_session_reference
from meridian.lib.ops.session_target import resolve_session_log_target
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from tests.integration.launch.test_pi_identity_launch import (
    context,
    install_shim,
    pi_runtime,  # noqa: F401
)
from tests.support.process_race import run_spawn_race_or_skip


@pytest.mark.parametrize(
    "shape",
    ["quit", "switch", "restart", "other", "missing", "truncated", "nonce", "pid", "oversize"],
)
def test_reader_final_quit_only(tmp_path: Path, shape: str) -> None:
    a = {"session_id": "entry", "session_file": "/store/1_entry.jsonl"}
    b = {"session_id": "exit", "session_file": "/store/2_exit.jsonl"}
    record = {
        "v": 2,
        "launch_nonce": "nonce",
        "pid": 100,
        "revision": 3,
        "initial": a,
        "current": b if shape == "switch" else a,
        "quit": b if shape == "switch" else a,
        "invalid_reason": None,
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
        path.write_text(
            "{"
            if shape == "truncated"
            else " " * 16385
            if shape == "oversize"
            else json.dumps(record)
        )
    result = read_boundary(path, nonce="nonce", pid=100)
    assert (result.exit is not None) == (shape in {"quit", "switch"})
    if shape == "switch":
        assert result.entry_observed is not None and result.entry_observed.session_id == "entry"
        assert result.exit is not None and result.exit.session_id == "exit"


@pytest.mark.parametrize("shape", ["quit", "restart", "eof", "exit", "eof-race"])
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
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
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
    if shape in {"quit", "eof"}:
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
    write_exit = (
        ":\n"
        if shape == "switch-missing"
        else ('printf \'{"type":"session","id":"%s"}\\n\' "$exit_id" > "$store/2_$exit_id.jsonl"\n')
    )
    publication = (
        f'exit_id="{exit_id}"\n'
        'if [ "$exit_id" != "$id" ]; then\n' + write_exit + "fi\n"
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
    if shape == "late-quit":
        # Pi abort stops the turn, not the RPC process. Quit is published only
        # during the later process shutdown, after terminal turn publication.
        shim.write_text(
            shim.read_text()
            .replace(
                "*) exit 0 ;;",
                "*) : ;;",
            )
            .replace(
                'if [ "$rpc" != "rpc" ]; then exit 0; fi',
                "publish_quit() {\n" + publication + "exit 0\n}\n"
                "trap publish_quit TERM\n"
                'if [ "$rpc" != "rpc" ]; then exit 0; fi',
            )
        )
    else:
        shim.write_text(
            shim.read_text().replace(
                'if [ "$rpc" != "rpc" ]; then exit 0; fi',
                publication + 'if [ "$rpc" != "rpc" ]; then exit 0; fi',
            )
        )


@pytest.mark.parametrize(
    "shape",
    ["same", "switch", "switch-missing", "restart", "mismatch", "truncated"],
)
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
    assert row.chat_id == outcome.chat_id
    assert entry.harness_session_id != "switched-id"
    assert outcome.exit_code == (1 if shape == "mismatch" else 0)
    expected = (
        "verified"
        if shape in {"same", "switch"}
        else "mismatch"
        if shape == "mismatch"
        else "unresolved"
    )
    assert (row.run_boundary.status if row.run_boundary else None) == expected
    meta = json.loads((runtime / "spawns" / row.id / "primary_meta.json").read_text())
    assert "exit_identity" not in meta
    if shape == "mismatch":
        assert_entry_mismatch(runtime, row.id, entry)
    if shape == "same":
        assert (row.run_boundary.exit_chat_id if row.run_boundary else None) == row.chat_id
    if shape == "switch":
        assert (row.run_boundary.exit_chat_id if row.run_boundary else None) != row.chat_id
        exit_chat_id = row.run_boundary.exit_chat_id if row.run_boundary else None
        exit_chat = session_store.get_session_record(runtime, exit_chat_id)
        assert exit_chat is not None and exit_chat.harness_session_id == "switched-id"
        spawn_reference = resolve_session_reference(
            root,
            row.id,
            runtime_root=runtime,
        )
        chat_reference = resolve_session_reference(
            root,
            entry.chat_id,
            runtime_root=runtime,
        )
        assert spawn_reference.authoritative_harness_session_id == "switched-id"
        assert chat_reference.authoritative_harness_session_id == entry.harness_session_id
    if shape == "switch-missing":
        assert row.run_boundary is not None and row.run_boundary.exit_chat_id is None
        assert row.chat_id == outcome.chat_id
        assert row.status == "succeeded"
        assert all(
            record.harness_session_id != "switched-id"
            for record in session_store.list_all_session_records(runtime)
        )
    if shape != "mismatch":
        target = resolve_session_log_target(
            ref=row.id,
            file_path=None,
            project_root=root,
            runtime_root=runtime,
        )
        expected_id = "switched-id" if shape == "switch" else entry.harness_session_id
        assert target.session_id == expected_id
        assert ("entry-based view" in target.source) == (
            shape in {"restart", "truncated", "switch-missing"}
        )


def _primary_cli(
    root: Path,
    *,
    continue_ref: str | None = None,
    fork_ref: str | None = None,
    dry_run: bool = False,
) -> PrimaryLaunchOutput:
    return run_primary_launch(
        project_root=root,
        continue_ref=continue_ref,
        fork_ref=fork_ref,
        fork_fresh_ref=None,
        model=None,
        harness="pi",
        agent=None,
        work="",
        task_dir=None,
        yolo=False,
        approval=None,
        autocompact=None,
        effort=None,
        sandbox=None,
        timeout=None,
        dry_run=dry_run,
        passthrough=(),
        prompt="hello",
    )


@pytest.mark.parametrize(
    "shape,expected_chat",
    [("switch", "exit"), ("switch-missing", "entry")],
)
def test_primary_cli_resume_hint_uses_verified_exit_or_entry_fallback(
    pi_runtime: Path,  # noqa: F811
    shape: str,
    expected_chat: str,
) -> None:
    root = pi_runtime
    install_boundary_shim(root, shape)

    output = _primary_cli(root)
    rendered = output.format_text()
    runtime = resolve_project_runtime_root_for_write(root)
    row = spawn_store.list_spawns(runtime).records[0]
    target_chat = (
        row.run_boundary.exit_chat_id
        if expected_chat == "exit" and row.run_boundary is not None
        else row.chat_id
    )
    assert target_chat is not None
    assert output.resume_command == f"meridian --continue {target_chat}"
    assert f"meridian --continue {target_chat}" in rendered


@pytest.mark.parametrize(
    "shape,expected_id",
    [
        ("switch", "switched-id"),
        ("switch-missing", "entry"),
    ],
)
def test_primary_cli_spawn_refs_project_exit_or_entry_native_key(
    pi_runtime: Path,  # noqa: F811
    shape: str,
    expected_id: str,
) -> None:
    root = pi_runtime
    install_boundary_shim(root, shape)
    runtime = resolve_project_runtime_root_for_write(root)
    _primary_cli(root)
    row = spawn_store.list_spawns(runtime).records[0]
    assert row is not None and row.chat_id is not None

    continued = _primary_cli(root, continue_ref=row.id, dry_run=True)
    forked = _primary_cli(root, fork_ref=row.id, dry_run=True)
    chat_continued = _primary_cli(root, continue_ref=row.chat_id, dry_run=True)

    target_chat_id = (
        row.run_boundary.exit_chat_id
        if shape == "switch" and row.run_boundary is not None
        else row.chat_id
    )
    target_record = session_store.get_session_record(runtime, target_chat_id or "")
    entry_record = session_store.get_session_record(runtime, row.chat_id)
    assert target_record is not None and target_record.native_store is not None
    assert entry_record is not None and entry_record.native_store is not None
    target_file = Path(target_record.native_store) / (
        f"2_{expected_id}.jsonl"
        if expected_id == "switched-id"
        else f"1_{target_record.harness_session_id}.jsonl"
    )
    entry_file = Path(entry_record.native_store) / f"1_{entry_record.harness_session_id}.jsonl"
    for projected in (continued, forked):
        assert str(target_file) in projected.command
        if shape == "switch":
            assert str(entry_file) not in projected.command
    assert str(entry_file) in chat_continued.command
    if shape == "switch":
        assert str(target_file) not in chat_continued.command


def assert_entry_mismatch(runtime: Path, spawn_id: str, entry: session_store.SessionRecord) -> None:
    row = spawn_store.get_spawn(runtime, spawn_id)
    assert row is not None and row.status == "failed"
    assert row.terminal is not None and row.terminal.error == "entry_mismatch"
    assert (row.run_boundary.status if row.run_boundary else None) == "mismatch"
    assert (row.run_boundary.exit_chat_id if row.run_boundary else None) is None
    facts = [
        json.loads(line)
        for line in (runtime / "spawns" / spawn_id / "runner-lifecycle.jsonl")
        .read_text()
        .splitlines()
    ]
    mismatch = [fact for fact in facts if fact["event"] == "entry_mismatch"]
    assert len(mismatch) == 1
    assert mismatch[0]["expected"] == {
        "harness": "pi",
        "native_store": entry.native_store,
        "session_id": entry.harness_session_id,
    }
    assert mismatch[0]["observed"] == {
        "harness": "pi",
        "native_store": entry.native_store,
        "session_id": "wrong-entry",
    }
    assert all(
        record.harness_session_id not in {"wrong-entry", "switched-id"}
        for record in session_store.list_all_session_records(runtime)
    )


def test_concurrent_exits_converge_on_one_stopped_chat(pi_runtime: Path) -> None:  # noqa: F811
    root = pi_runtime
    install_shim(root)
    outcome = run_harness_process(context(root), HarnessRegistry.with_defaults())
    assert outcome.chat_id is not None
    args = (root / ".meridian", outcome.chat_id, "pi", "/exit-store", "exit-id")
    ids = run_spawn_race_or_skip(_allocate_test_exit_chat, [args, args])
    assert ids[0] == ids[1] != outcome.chat_id
    record = session_store.get_session_record(root / ".meridian", ids[0])
    assert record is not None and record.stopped_at is not None


def _allocate_test_exit_chat(
    runtime: Path,
    entry: str,
    harness: str,
    native_store: str,
    session_id: str,
) -> str | None:
    return session_store.get_or_create_exit_chat(
        runtime,
        entry,
        harness,
        native_store,
        session_id,
        native_exists=lambda: True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "shape",
    [
        "switch",
        "mismatch",
        "header-missing",
        "header-poisoned",
        "late-quit",
        "connection-switch",
    ],
)
async def test_rpc_post_attempt_boundary(
    pi_runtime: Path,  # noqa: F811
    shape: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    if shape == "connection-switch":
        from meridian.lib.harness.connections.pi_rpc import PiRpcConnection

        original_stop = PiRpcConnection.stop

        async def stop(connection, **kwargs):
            result = await original_stop(connection, **kwargs)
            connection._session_id = "switched-id"
            return result

        monkeypatch.setattr(PiRpcConnection, "stop", stop)
    if shape.startswith("header-"):
        shim = root.parent / "fake-bin" / "pi"
        text = shim.read_text().replace("header_id=$id", "header_id=wrong-entry")
        if shape == "header-missing":
            text = text.replace(
                'if [ "$rpc" != "rpc" ]; then exit 0; fi',
                'rm "$_MERIDIAN_PI_SESSION_BOUNDARY_PATH"\nif [ "$rpc" != "rpc" ]; then exit 0; fi',
            )
        else:
            text = text.replace('"invalid_reason":null', '"invalid_reason":"poison"')
        shim.write_text(text)
    ctx = context(root, primary=False)
    run = Spawn(spawn_id=SpawnId("p42"), prompt="hello", model=ModelId("pi-test"), status="queued")
    spawn_store.start_spawn(
        ctx.runtime_root,
        spawn_id=run.spawn_id,
        chat_id="",
        model="pi-test",
        agent="",
        harness="pi",
        kind="streaming",
        prompt="hello",
        status="queued",
    )
    env = dict(ctx.binding.environment.final_env)
    prepared = ctx.harness.prepare_prelaunch(
        runtime_root=ctx.runtime_root,
        spawn_id=SpawnId("p42"),
        session=ctx.request.session,
        child_cwd=root,
        child_env=env,
        resolved_harness_session_id="",
    )
    env.update(prepared.env_overrides)
    ctx = replace(
        ctx,
        binding=replace(
            ctx.binding,
            environment=replace(
                ctx.binding.environment,
                final_env=MappingProxyType(env),
            ),
        ),
    )
    with session_scope(
        runtime_root=ctx.runtime_root,
        metadata=PrimarySessionMetadata(
            harness="pi",
            model="pi-test",
            agent="",
            agent_path="",
            skills=(),
            skill_paths=(),
        ),
        request=ctx.request.session,
        spawn_id="p42",
        startup_attempt_id="boundary-test",
    ) as managed:
        code = await asyncio.wait_for(
            execute_with_streaming(
                run,
                request=ctx.request,
                launch_context=ctx,
                project_root=root,
                runtime_root=ctx.runtime_root,
                artifacts=LocalStore(root_dir=ctx.runtime_root / "artifacts"),
                session_attempt=managed,
            ),
            20,
        )
        assert code == (0 if shape in {"switch", "late-quit", "connection-switch"} else 1)
        entry = session_store.get_session_record(ctx.runtime_root, managed.chat_id)
        assert entry is not None and entry.harness_session_id not in {"wrong-entry", "switched-id"}
        if shape == "mismatch":
            assert_entry_mismatch(ctx.runtime_root, "p42", entry)
            return
        if shape.startswith("header-"):
            row = spawn_store.get_spawn(ctx.runtime_root, "p42")
            assert row is not None and row.terminal is not None
            assert row.terminal.error == "entry_mismatch"
            assert row.run_boundary is not None
            assert row.run_boundary.exit_chat_id is None
            assert row.run_boundary.status == "mismatch"
            events = [
                json.loads(line)
                for line in (ctx.runtime_root / "sessions.jsonl").read_text().splitlines()
            ]
            assert not any(event.get("kind") == "invocation_started" for event in events)
            return
        row = spawn_store.get_spawn(ctx.runtime_root, "p42")
        assert row is not None and row.run_boundary is not None
        assert row.run_boundary.status == "verified"
        assert row.chat_id == managed.chat_id
        assert row.run_boundary.exit_chat_id != managed.chat_id


@pytest.mark.parametrize("boundary", ["missing", "poisoned", "valid"])
def test_primary_header_mismatch_prevents_exit_attribution(
    pi_runtime: Path,  # noqa: F811
    boundary: str,
) -> None:
    root = pi_runtime
    if boundary == "missing":
        install_shim(root, behavior="mismatch")
    else:
        install_boundary_shim(root, "switch")
        shim = root.parent / "fake-bin" / "pi"
        text = shim.read_text().replace("header_id=$id", "header_id=wrong-entry")
        if boundary == "poisoned":
            text = text.replace('"invalid_reason":null', '"invalid_reason":"poison"')
        shim.write_text(text)
    outcome = run_harness_process(context(root), HarnessRegistry.with_defaults())
    runtime = root / ".meridian"
    assert outcome.primary_spawn_id is not None
    row = spawn_store.get_spawn(runtime, outcome.primary_spawn_id)
    assert row is not None and row.terminal is not None
    assert row.terminal.error == "entry_mismatch"
    assert (row.run_boundary.exit_chat_id if row.run_boundary else None) is None
    facts = [
        json.loads(line)
        for line in (runtime / "spawns" / row.id / "runner-lifecycle.jsonl")
        .read_text()
        .splitlines()
    ]
    assert any(
        fact["event"] == "entry_mismatch" and fact["expected"] != fact["observed"] for fact in facts
    )
    events = [json.loads(line) for line in (runtime / "sessions.jsonl").read_text().splitlines()]
    assert not any(event.get("kind") == "invocation_started" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["launch-failure", "streaming-except", "post-exit"])
async def test_identity_failure_payload_is_identical_at_every_boundary(
    pi_runtime: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    trigger: str,
) -> None:
    from meridian.lib.core.domain import Spawn
    from meridian.lib.core.native_identity import NativeEntryMismatch, NativeKeyFields
    from meridian.lib.core.types import ModelId, SpawnId
    from meridian.lib.launch import streaming_runner
    from meridian.lib.ops.spawn.failure_policy import finalize_launch_failure
    from meridian.lib.state.artifact_store import LocalStore

    root = pi_runtime
    install_shim(root)
    ctx = context(root, primary=False)
    run = Spawn(spawn_id=SpawnId("p42"), prompt="hello", model=ModelId("pi-test"), status="queued")
    spawn_store.start_spawn(
        ctx.runtime_root,
        spawn_id=run.spawn_id,
        chat_id="",
        model="pi-test",
        agent="",
        harness="pi",
        kind="streaming",
        prompt="hello",
        status="queued",
    )
    expected = {"harness": "pi", "native_store": "/native/store", "session_id": "entry"}
    observed = {**expected, "session_id": "wrong-entry"}
    error = NativeEntryMismatch(NativeKeyFields(**expected), NativeKeyFields(**observed))
    if trigger == "launch-failure":
        await finalize_launch_failure(ctx.runtime_root, root, run.spawn_id, error)
    else:
        from meridian.lib.core.native_identity import PostExit
        from meridian.lib.launch.session_scope import SessionAttempt

        def verify(*args, **kwargs) -> PostExit:
            if trigger == "streaming-except":
                raise error
            return PostExit(entry_error=error)

        monkeypatch.setattr(ctx.harness, "observe_after_exit", verify)
        chat_id = session_store.start_session(ctx.runtime_root, "pi", "", "")
        record = session_store.get_session_record(ctx.runtime_root, chat_id)
        assert record is not None
        spawn_store.update_spawn(ctx.runtime_root, run.spawn_id, chat_id=chat_id)
        code = await streaming_runner.execute_with_streaming(
            run,
            request=ctx.request,
            launch_context=ctx,
            project_root=root,
            runtime_root=ctx.runtime_root,
            artifacts=LocalStore(root_dir=ctx.runtime_root / "artifacts"),
            session_attempt=SessionAttempt(
                ctx.runtime_root, chat_id, record.session_instance_id, "attempt-1", run.spawn_id
            ),
        )
        assert code == 1
    facts = [
        json.loads(line)
        for line in (ctx.runtime_root / "spawns" / "p42" / "runner-lifecycle.jsonl")
        .read_text()
        .splitlines()
    ]
    payloads = [fact for fact in facts if fact["event"] == "entry_mismatch"]
    assert len(payloads) == 1
    payload = payloads[0]
    assert {key: payload[key] for key in ("event", "expected", "observed", "reason", "detail")} == {
        "event": "entry_mismatch",
        "expected": expected,
        "observed": observed,
        "reason": "key",
        "detail": None,
    }
    row = spawn_store.get_spawn(ctx.runtime_root, "p42")
    assert row is not None and row.status == "failed" and row.terminal is not None
    assert row.terminal.error == "entry_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["same", "late-quit", "header-mismatch"])
async def test_streaming_serve_concludes_native_identity(
    pi_runtime: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    import asyncio

    from meridian.cli import streaming_serve as serve
    from meridian.lib.core.native_identity import NativeEntryMismatch
    from meridian.lib.ops.runtime import resolve_runtime_root

    root = pi_runtime
    install_boundary_shim(root, "late-quit" if shape == "late-quit" else "same")
    if shape == "header-mismatch":
        shim = root.parent / "fake-bin" / "pi"
        shim.write_text(shim.read_text().replace("header_id=$id", "header_id=wrong-entry"))
    monkeypatch.setattr(serve, "require_established_project_root", lambda: root)
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", str(root))
    runtime = resolve_runtime_root(root)
    original_conclude = serve.conclude_native_run

    def conclude(*args, **kwargs):
        events = [
            json.loads(line) for line in (runtime / "sessions.jsonl").read_text().splitlines()
        ]
        assert not any(event.get("kind") == "invocation_started" for event in events)
        return original_conclude(*args, **kwargs)

    monkeypatch.setattr(serve, "conclude_native_run", conclude)
    if shape == "header-mismatch":
        with pytest.raises(NativeEntryMismatch):
            await asyncio.wait_for(serve.streaming_serve("pi", "hello", model="pi-test"), 20)
    else:
        await asyncio.wait_for(serve.streaming_serve("pi", "hello", model="pi-test"), 20)
    row = spawn_store.get_spawn(runtime, "p1")
    assert row is not None and row.run_boundary is not None
    assert row.run_boundary.status == ("mismatch" if shape == "header-mismatch" else "verified")
    events = [json.loads(line) for line in (runtime / "sessions.jsonl").read_text().splitlines()]
    starts = [event for event in events if event.get("kind") == "invocation_started"]
    assert len(starts) == (0 if shape == "header-mismatch" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("conclusion_raises", [False, True])
@pytest.mark.parametrize("run_fails", [False, True])
async def test_serve_keeps_run_error_and_cleans_up_after_conclusion(
    pi_runtime: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    conclusion_raises: bool,
    run_fails: bool,
) -> None:
    from meridian.cli import streaming_serve as serve
    from meridian.lib.core.native_identity import NativeEntryMismatch, NativeKeyFields
    from meridian.lib.harness.pi import PiAdapter
    from meridian.lib.launch.native_run import NativeRunOutcome
    from meridian.lib.state.spawn.model import RunBoundaryOutcome

    install_boundary_shim(pi_runtime, "same")
    monkeypatch.setattr(serve, "require_established_project_root", lambda: pi_runtime)
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", str(pi_runtime))
    run_error = RuntimeError("transport failed first")
    identity_error = NativeEntryMismatch(
        NativeKeyFields(session_id="a"), NativeKeyFields(session_id="b")
    )
    order = []

    async def run(**kwargs):
        order.append("run")
        if run_fails:
            raise run_error
        from types import SimpleNamespace

        return SimpleNamespace(status="succeeded", exit_code=0)

    def conclude(*args, **kwargs):
        order.append("conclude")
        if conclusion_raises:
            raise identity_error
        return NativeRunOutcome(identity_error, RunBoundaryOutcome(status="mismatch"), None)

    original_cleanup = PiAdapter.cleanup_prelaunch

    def cleanup(self, **kwargs):
        order.append("cleanup")
        original_cleanup(self, **kwargs)

    monkeypatch.setattr(serve, "run_streaming_spawn", run)
    monkeypatch.setattr(serve, "conclude_native_run", conclude)
    monkeypatch.setattr(PiAdapter, "cleanup_prelaunch", cleanup)
    with pytest.raises(RuntimeError if run_fails else NativeEntryMismatch) as caught:
        await serve.streaming_serve("pi", "hello", model="pi-test")
    assert caught.value is (run_error if run_fails else identity_error)
    assert order == ["run", "conclude", "cleanup"]
