"""Managed Pi launches use assigned native keys, never newest-file discovery."""

from __future__ import annotations

import asyncio
import json
import shlex
from pathlib import Path

import pytest

from meridian.lib.core.domain import Spawn
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.types import HarnessId, ModelId, SpawnId
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.process.runner import run_harness_process
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SessionRequest,
    SpawnRequest,
)
from meridian.lib.launch.session_scope import session_scope
from meridian.lib.launch.streaming_runner import execute_with_streaming
from meridian.lib.launch.types import PrimarySessionMetadata
from meridian.lib.state import session_store, spawn_store
from meridian.lib.state.artifact_store import LocalStore
from tests.support.executables import prepend_fake_executables
from tests.support.launch import stub_bundle_request_and_resolve
from tests.support.pi_extensions import configure_pi_extension_projection

HELP = (
    "--mode rpc --model --append-system-prompt --session --session-id --fork "
    "--session-dir --no-extensions --no-skills --no-context-files --no-prompt-templates "
    "-e --extension PI_CODING_AGENT_SESSION_DIR"
)


@pytest.fixture
def pi_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "meridian-home"))
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    monkeypatch.delenv("MERIDIAN_PI_BINARY", raising=False)
    configure_pi_extension_projection(monkeypatch, tmp_path)
    stub_bundle_request_and_resolve(monkeypatch, model="pi-test", harness=HarnessId.PI)
    prepend_fake_executables(monkeypatch, tmp_path, "pi")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "mars.toml").write_text('[settings]\ntargets = [".pi"]\n')
    return root


def install_shim(root: Path, *, behavior: str = "ok") -> None:
    """Only external Pi is fake; bind, argv, journal, transport and exit are real."""
    scratch = root.parent
    shim = scratch / "fake-bin" / "pi"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "0.87.1"; exit 0; fi\n'
        f'if [ "$1" = "--help" ]; then echo {shlex.quote(HELP)}; exit 0; fi\n'
        f"cp {shlex.quote(str(root / '.meridian' / 'sessions.jsonl'))} "
        f"{shlex.quote(str(scratch / 'binding-at-exec'))}\n"
        f'printf "%s\\n" "$@" > {shlex.quote(str(scratch / "argv"))}\n'
        'printf "%s\\n" "$PI_CODING_AGENT_SESSION_DIR" > '
        f'{shlex.quote(str(scratch / "env-store"))}\n'
        "id=; source=; session=; rpc=\n"
        'while [ "$#" -gt 0 ]; do\n'
        ' case "$1" in\n'
        " --session-id) shift; id=$1 ;;\n"
        " --session-dir) shift; store=$1 ;;\n"
        " --fork) shift; source=$1 ;;\n"
        " --session) shift; session=$1 ;;\n"
        " --mode) shift; rpc=$1 ;;\n"
        " esac; shift\ndone\n"
        + ("exit 7\n" if behavior == "fail" else "")
        + 'mkdir -p "$store"\n'
        + ("header_id=replacement\n" if behavior == "mismatch" else "header_id=$id\n")
        + ("source=wrong-parent\n" if behavior == "wrong-parent" else "")
        + 'if [ -n "$id" ]; then\n'
        ' printf \'{"type":"session","id":"%s","parentSession":"%s"}\\n\' '
        '"$header_id" "$source" > "$store/1_$id.jsonl"\nfi\n'
        # Concurrent unrelated writer must not influence the bound key.
        'printf \'{"type":"session","id":"unrelated-newest"}\\n\''
        ' > "$store/9_unrelated-newest.jsonl"\n'
        + (
            'if [ -n "$session" ]; then printf \'{"type":"session","id":"replacement"}\\n\' '
            '> "$session"; fi\n'
            if behavior == "mismatch"
            else ""
        )
        + 'if [ "$rpc" != "rpc" ]; then exit 0; fi\n'
        "while IFS= read -r line; do\n"
        ' case "$line" in\n'
        ' *\'"type":"prompt"\'*)\n'
        " printf '%s\\n' '{\"type\":\"agent_start\"}' "
        '\'{"type":"agent_end","messages":[{"role":"assistant","stopReason":"stop",'
        '"content":[{"type":"text","text":"done"}]}]}\' ;;\n'
        ' *\'"type":"abort"\'*) exit 0 ;;\n'
        " esac\ndone\n"
    )


def context(
    root: Path,
    *,
    primary: bool = True,
    spawn_id: str = "p42",
    session: SessionRequest | None = None,
    extra_args: tuple[str, ...] = (),
):
    request = SpawnRequest(
        harness="pi",
        model="pi-test",
        prompt="hello",
        session=session or SessionRequest(),
        extra_args=extra_args,
        launch_policy_snapshot=(
            LaunchPolicySnapshot(model="pi-test", harness="pi") if session else None
        ),
    )
    return build_launch_context(
        spawn_id=spawn_id,
        request=request,
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=(
                LaunchCompositionSurface.PRIMARY if primary else LaunchCompositionSurface.DIRECT
            ),
            runtime_root=str(root / ".meridian"),
            project_paths_project_root=str(root),
            project_paths_execution_cwd=str(root),
        ),
        harness_registry=HarnessRegistry.with_defaults(),
        dry_run=primary,
    )


def assert_prebound(root: Path, chat_id: str) -> tuple[str, Path]:
    argv = (root.parent / "argv").read_text().splitlines()
    native_id = argv[argv.index("--session-id") + 1]
    store = Path(argv[argv.index("--session-dir") + 1])
    record = session_store.get_session_record(root / ".meridian", chat_id)
    assert record is not None
    assert record.harness_session_id == native_id
    assert record.native_store == str(store)
    assert (root.parent / "env-store").read_text().strip() == str(store)
    at_exec = [
        json.loads(line) for line in (root.parent / "binding-at-exec").read_text().splitlines()
    ]
    assert any(
        event.get("harness_session_id") == native_id and event.get("native_store") == str(store)
        for event in at_exec
    )
    return native_id, store


@pytest.mark.parametrize("behavior", ["ok", "fail", "mismatch"])
def test_primary_assigns_before_exec_and_verifies_exact_entry(
    pi_runtime: Path, behavior: str
) -> None:
    root = pi_runtime
    install_shim(root, behavior=behavior)
    outcome = run_harness_process(context(root), HarnessRegistry.with_defaults())
    assert outcome.chat_id is not None
    native_id, store = assert_prebound(root, outcome.chat_id)
    assert outcome.exit_code == (0 if behavior == "ok" else 7 if behavior == "fail" else 1)
    row = spawn_store.get_spawn(root / ".meridian", outcome.primary_spawn_id)
    assert row is not None
    assert row.status == ("succeeded" if behavior == "ok" else "failed")
    meta = json.loads(
        (
            root / ".meridian" / "spawns" / str(outcome.primary_spawn_id) / "primary_meta.json"
        ).read_text()
    )
    assert meta["exit_identity"] == ("mismatch" if behavior == "mismatch" else "unresolved")
    if behavior == "fail":
        with pytest.raises(ValueError, match="native_transcript_missing"):
            context(
                root,
                session=SessionRequest(
                    requested_harness_session_id=native_id,
                    continue_chat_id=outcome.chat_id,
                    source_native_store=str(store),
                    primary_session_mode="resume",
                ),
            )


@pytest.mark.parametrize("fork", [False, True])
@pytest.mark.parametrize("behavior", ["ok", "mismatch", "wrong-parent"])
def test_resume_and_fork_use_verified_absolute_source(
    pi_runtime: Path,
    fork: bool,
    behavior: str,
) -> None:
    root = pi_runtime
    install_shim(root)
    first = run_harness_process(context(root), HarnessRegistry.with_defaults())
    assert first.chat_id is not None
    native_id, store = assert_prebound(root, first.chat_id)
    before = session_store.get_session_record(root / ".meridian", first.chat_id)
    source = store / f"1_{native_id}.jsonl"
    install_shim(root, behavior=behavior)
    outcome = run_harness_process(
        context(
            root,
            session=SessionRequest(
                requested_harness_session_id=native_id,
                continue_chat_id=first.chat_id,
                source_native_store=str(store),
                continue_fork=fork,
                primary_session_mode="fork" if fork else "resume",
            ),
        ),
        HarnessRegistry.with_defaults(),
    )
    argv = (root.parent / "argv").read_text().splitlines()
    assert argv[argv.index("--fork" if fork else "--session") + 1] == str(source)
    assert native_id not in argv
    if fork:
        assert outcome.chat_id != first.chat_id
        assert outcome.chat_id is not None
        new_id, _ = assert_prebound(root, outcome.chat_id)
        assert new_id != native_id
    else:
        assert "--session-id" not in argv
    after = session_store.get_session_record(root / ".meridian", first.chat_id)
    assert before is not None and after is not None
    assert (after.harness_session_id, after.native_store) == (
        before.harness_session_id,
        before.native_store,
    )
    assert outcome.exit_code == (
        1 if behavior == "mismatch" or (fork and behavior == "wrong-parent") else 0
    )


@pytest.mark.parametrize("primary", [True, False])
@pytest.mark.parametrize(
    "flag",
    [
        "--session-id",
        "--session",
        "--fork",
        "-c",
        "--continue",
        "-r",
        "--resume",
        "--session-dir",
        "--no-session",
    ],
)
def test_managed_passthrough_refuses_identity_flags(
    pi_runtime: Path, primary: bool, flag: str
) -> None:
    install_shim(pi_runtime)
    with pytest.raises(ValueError, match="Pi"):
        context(pi_runtime, primary=primary, extra_args=(flag,))
    assert not (pi_runtime.parent / "argv").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("behavior", ["ok", "fail", "mismatch"])
async def test_rpc_spawn_uses_prebound_scoped_store(pi_runtime: Path, behavior: str) -> None:
    root = pi_runtime
    install_shim(root, behavior=behavior)
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
    with session_scope(
        runtime_root=ctx.runtime_root,
        metadata=PrimarySessionMetadata(
            harness="pi", model="pi-test", agent="", agent_path="", skills=(), skill_paths=()
        ),
        request=ctx.request.session,
        harness_session_id="",
        spawn_id="p42",
        startup_attempt_id="test-attempt",
    ) as managed:
        code = await asyncio.wait_for(
            execute_with_streaming(
                run,
                request=ctx.request,
                launch_context=ctx,
                project_root=root,
                runtime_root=ctx.runtime_root,
                artifacts=LocalStore(root_dir=ctx.runtime_root / "artifacts"),
                session_attempt=managed.attempt,
            ),
            20,
        )
        native_id, store = assert_prebound(root, managed.chat_id)
        assert store.name == "p42"
        assert (code == 0) == (behavior == "ok")
        row = spawn_store.get_spawn(ctx.runtime_root, run.spawn_id)
        assert row is not None
        assert row.status == ("succeeded" if behavior == "ok" else "failed")
        if behavior == "fail":
            with pytest.raises(ValueError, match="native_transcript_missing"):
                context(
                    root,
                    primary=False,
                    session=SessionRequest(
                        requested_harness_session_id=native_id,
                        source_native_store=str(store),
                    ),
                )


@pytest.mark.parametrize("primary", [True, False])
def test_collision_refuses_before_exec(
    pi_runtime: Path, monkeypatch: pytest.MonkeyPatch, primary: bool,
) -> None:
    install_shim(pi_runtime)
    planned = context(pi_runtime, primary=primary).binding.spec.native_identity_plan
    assert planned is not None and planned.native_store is not None
    store = Path(planned.native_store)
    store.mkdir(parents=True, exist_ok=True)
    (store / "unrelated-basename.jsonl").write_text('{"type":"session","id":"chosen-id"}\n')
    monkeypatch.setattr("meridian.lib.harness.pi_identity.uuid.uuid4", lambda: "chosen-id")
    with pytest.raises(ValueError, match="native_identity_collision"):
        context(pi_runtime, primary=primary)
    assert not (pi_runtime.parent / "argv").exists()


@pytest.mark.asyncio
async def test_spawn_continue_reuses_chat_and_fork_allocates_new_chat(pi_runtime: Path) -> None:
    from meridian.lib.ops.spawn.execute_session import _session_execution_context

    root = pi_runtime
    install_shim(root)
    source = None
    for number, operation in enumerate(("create", "resume", "fork"), 42):
        session = SessionRequest() if source is None else SessionRequest(
            requested_harness_session_id=source.harness_session_id,
            continue_chat_id=source.chat_id,
            continue_source_ref=source.chat_id,
            continue_source_tracked=True,
            source_native_store=source.native_store,
            continue_fork=operation == "fork",
        )
        ctx = context(root, primary=False, spawn_id=f"p{number}", session=session)
        run = Spawn(
            spawn_id=SpawnId(f"p{number}"), prompt="hello",
            model=ModelId("pi-test"), status="queued",
        )
        spawn_store.start_spawn(
            ctx.runtime_root, spawn_id=run.spawn_id, chat_id="", model="pi-test",
            agent="", harness="pi", kind="streaming", prompt="hello", status="queued",
        )
        with _session_execution_context(
            runtime_root=ctx.runtime_root,
            metadata=PrimarySessionMetadata(
                harness="pi", model="pi-test", agent="", agent_path="", skills=(), skill_paths=()
            ),
            request=session,
            harness_session_id=(session.requested_harness_session_id or "")
            if operation == "resume" else "",
            run_agent_name=None,
            spawn_id=str(run.spawn_id),
        ) as managed:
            code = await asyncio.wait_for(
                execute_with_streaming(
                    run, request=ctx.request, launch_context=ctx, project_root=root,
                    runtime_root=ctx.runtime_root,
                    artifacts=LocalStore(root_dir=ctx.runtime_root / "artifacts"),
                    session_attempt=managed.attempt,
                ),
                20,
            )
            assert code == 0
            record = session_store.get_session_record(ctx.runtime_root, managed.chat_id)
            assert record is not None
            if source is None:
                source = record
                assert source.chat_id == "c1"
            elif operation == "resume":
                assert record.chat_id == source.chat_id == "c1"
                assert (record.harness_session_id, record.native_store) == (
                    source.harness_session_id, source.native_store,
                )
            else:
                assert record.chat_id == "c2"
                assert record.harness_session_id != source.harness_session_id
                unchanged = session_store.get_session_record(ctx.runtime_root, source.chat_id)
                assert unchanged is not None
                assert (unchanged.harness_session_id, unchanged.native_store) == (
                    source.harness_session_id, source.native_store,
                )



def test_primary_create_with_unreadable_sibling_warns_and_executes(pi_runtime: Path) -> None:
    from structlog.testing import capture_logs

    install_shim(pi_runtime)
    planned = context(pi_runtime).binding.spec.native_identity_plan
    assert planned is not None and planned.native_store is not None
    store = Path(planned.native_store)
    store.mkdir(parents=True, exist_ok=True)
    unreadable = store / "torn.jsonl"
    unreadable.write_text("")
    with capture_logs() as logs:
        outcome = run_harness_process(context(pi_runtime), HarnessRegistry.with_defaults())
    assert outcome.exit_code == 0
    assert outcome.chat_id is not None
    assert_prebound(pi_runtime, outcome.chat_id)
    assert any(
        event.get("event") == "pi_store_unreadable_header"
        and event.get("path") == str(unreadable)
        for event in logs
    )
    assert unreadable.read_text() == ""
