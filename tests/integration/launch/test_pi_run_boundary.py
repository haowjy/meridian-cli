"""Run exit identity is distinct from immutable entry ownership."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from meridian.cli.primary_launch import PrimaryLaunchOutput, run_primary_launch
from meridian.lib.harness.pi_boundary import read_boundary
from meridian.lib.state import session_store, spawn_store
from tests.conftest import requires_pi_dist
from tests.integration.launch.test_pi_identity_launch import (
    install_shim,
    pi_runtime,  # noqa: F401
)
from tests.support.launch_paths import launch_paths


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


@requires_pi_dist
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


def assert_entry_mismatch(runtime: Path, spawn_id: str, entry: session_store.SessionRecord) -> None:
    row = spawn_store.get_spawn(runtime, spawn_id)
    assert row is not None and row.status == "failed"
    assert row.terminal is not None and row.terminal.error == "entry_mismatch"
    assert (row.run_boundary.status if row.run_boundary else None) == "mismatch"
    assert row.current_chat_id is None
    facts = [
        json.loads(line)
        for line in launch_paths(runtime, spawn_id).runner_lifecycle.read_text().splitlines()
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


def _allocate_test_exit_chat(
    runtime: Path,
    entry: str,
    harness: str,
    native_store: str,
    session_id: str,
    spawn_id: str,
) -> str | None:
    return session_store.get_or_create_observed_chat(
        runtime,
        entry,
        harness,
        native_store,
        session_id,
        spawn_id=spawn_id,
        native_exists=lambda: True,
    )


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
    monkeypatch.setattr(serve, "resolve_cli_project_root", lambda: pi_runtime)
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
