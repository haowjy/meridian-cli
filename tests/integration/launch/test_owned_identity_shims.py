"""Native process output binds an owned ID and its finalized namespace together."""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.process.runner import run_harness_process
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SpawnRequest,
)
from meridian.lib.state import session_store
from tests.support.executables import prepend_fake_executables
from tests.support.launch import stub_bundle_request_and_resolve

NATIVE_ID = "12345678-1234-4234-8234-123456789abc"


@pytest.mark.parametrize("signal", ["owned", "prose", "nested", "none"])
@pytest.mark.parametrize("harness", [HarnessId.CODEX, HarnessId.OPENCODE])
def test_owned_event_pins_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: HarnessId, signal: str
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "meridian-home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("OPENCODE_HOME", str(tmp_path / "opencode"))
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    stub_bundle_request_and_resolve(monkeypatch, model="openai/test-model", harness=harness)
    prepend_fake_executables(monkeypatch, tmp_path, str(harness))
    event = (
        {"type": "thread.started", "thread_id": NATIVE_ID}
        if harness == HarnessId.CODEX
        else {"type": "text", "sessionID": NATIVE_ID, "part": {"text": "done"}}
    )
    if signal != "owned":
        event = {"type": "item.completed", "item": {"type": "agent_message"}}
        if signal == "prose":
            event["item"]["text"] = f"Example: codex resume {NATIVE_ID}"
        elif signal == "nested":
            event["item"]["session_id"] = NATIVE_ID
    (tmp_path / "fake-bin" / str(harness)).write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "1.0.0"; exit 0; fi\n'
        f"printf '%s\\n' {shlex.quote(json.dumps(event))}\n"
    )
    root = tmp_path / "repo"
    root.mkdir()
    (root / "mars.toml").write_text(f'[settings]\ntargets = [".{harness}"]\n')
    registry = HarnessRegistry.with_defaults()
    context = build_launch_context(
        spawn_id="p42",
        request=SpawnRequest(harness=str(harness), model="openai/test-model", prompt="hello"),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.PRIMARY,
            runtime_root=str(root / ".meridian"),
            project_paths_project_root=str(root),
            project_paths_execution_cwd=str(root),
        ),
        harness_registry=registry,
        dry_run=True,
    )
    from meridian.lib.harness.bundle import get_harness_bundle
    from meridian.lib.harness.connections.base import RawHarnessEvent
    from meridian.lib.launch.process.primary_attach import PrimaryAttachOutcome

    def attach(
        harness_id,
        spawn_id,
        log_dir,
        control_root,
        task_cwd,
        env,
        spec,
        launcher,
        on_running,
        session_id_observer,
    ):
        # Replace the external backend/TUI transport, not identity parsing or persistence.
        process = subprocess.Popen(
            [str(tmp_path / "fake-bin" / str(harness_id))],
            env=env,
            cwd=task_cwd or control_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        on_running(process.pid)
        stdout, _stderr = process.communicate(timeout=5)
        assert process.returncode == 0
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "output.jsonl").write_text(stdout)
        payload = json.loads(stdout)
        native_id = get_harness_bundle(harness_id).extractor.detect_session_id_from_event(
            RawHarnessEvent(event_type=payload["type"], harness_id=str(harness_id), payload=payload)
        )
        if native_id:
            session_id_observer(native_id)
        return PrimaryAttachOutcome(exit_code=0, session_id=native_id, tui_pid=None)

    outcome = run_harness_process(context, registry, run_primary_attach_fn=attach)
    assert outcome.exit_code == 0
    assert outcome.chat_id
    record = session_store.get_session_record(root / ".meridian", outcome.chat_id)
    assert record
    if signal != "owned":
        assert not record.harness_session_id
        assert record.native_store is None
        return
    assert record.harness_session_id == NATIVE_ID
    assert record.native_store == str(
        tmp_path / ("codex/sessions" if harness == HarnessId.CODEX else "opencode/opencode.db")
    )
    store = Path(record.native_store)
    native_file = (
        store / f"rollout-2026-01-01T00-00-00-{NATIVE_ID}.jsonl"
        if harness == HarnessId.CODEX
        else store
    )
    native_file.parent.mkdir(parents=True)
    if harness == HarnessId.CODEX:
        native_file.write_text(
            json.dumps({"type": "session_meta", "payload": {"id": NATIVE_ID}}) + "\n",
        )
    else:
        from tests.support.opencode_db import write_opencode_db_session

        write_opencode_db_session(db_path=store, session_id=NATIVE_ID, messages=[])
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("OPENCODE_HOME", str(tmp_path / "elsewhere"))
    assert (
        registry.get(harness).resolve_native_session_file(session_id=NATIVE_ID, native_store=store)
        == native_file
    )


@pytest.mark.parametrize("harness", [HarnessId.CLAUDE, HarnessId.OPENCODE])
def test_blackbox_fork_cannot_reuse_source(tmp_path, monkeypatch, harness):
    from meridian.lib.harness.adapter import BootstrapMode
    from meridian.lib.launch.request import SessionRequest
    from meridian.lib.state import spawn_store
    from tests.support.opencode_db import write_opencode_db_session

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "meridian-home"))
    stub_bundle_request_and_resolve(monkeypatch, model="openai/test-model", harness=harness)
    prepend_fake_executables(monkeypatch, tmp_path, str(harness))
    source_id = "ses_fork_source" if harness == HarnessId.OPENCODE else NATIVE_ID
    root = tmp_path / "repo"
    root.mkdir()
    (root / "mars.toml").write_text(f'[settings]\ntargets = [".{harness}"]\n')
    store = tmp_path / "source"
    if harness == HarnessId.CLAUDE:
        store.mkdir()
        (store / f"{source_id}.jsonl").write_text(json.dumps({"sessionId": source_id}) + "\n")
        event = {"type": "system", "subtype": "init", "session_id": source_id}
    else:
        store = tmp_path / "opencode.db"
        write_opencode_db_session(db_path=store, session_id=source_id, messages=[])
        event = {"type": "text", "sessionID": source_id, "part": {"text": "done"}}
    at_exec = tmp_path / "at-exec"
    shim = tmp_path / "fake-bin" / str(harness)
    shim.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then echo 1.0; exit 0; fi\n'
        f'cp "{root}/.meridian/sessions.jsonl" "{at_exec}"\n'
        f"printf '%s\\n' {shlex.quote(json.dumps(event))}\n"
    )
    registry = HarnessRegistry.with_defaults()
    adapter = registry.get(harness)
    capabilities = adapter.capabilities.model_copy(
        update={"supports_session_fork": True, "captures_blackbox_output": True}
    )
    monkeypatch.setattr(type(adapter), "capabilities", property(lambda self: capabilities))
    contract = adapter.contract
    monkeypatch.setattr(
        type(adapter),
        "contract",
        property(
            lambda self: contract.model_copy(
                update={
                    "capabilities": contract.capabilities.model_copy(
                        update={"captures_blackbox_output": True}
                    ),
                    "bootstrap": contract.bootstrap.model_copy(
                        update={"mode": BootstrapMode.SUBPROCESS_ONLY}
                    ),
                }
            )
        ),
    )
    ctx = build_launch_context(
        spawn_id="preview",
        request=SpawnRequest(
            harness=str(harness),
            model="openai/test-model",
            prompt="hello",
            extra_args=("--print",),
            session=SessionRequest(
                requested_harness_session_id=source_id,
                continue_fork=True,
                source_native_store=str(store),
            ),
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.PRIMARY,
            runtime_root=str(root / ".meridian"),
            project_paths_project_root=str(root),
            project_paths_execution_cwd=str(root),
        ),
        harness_registry=registry,
        dry_run=True,
    )
    outcome = run_harness_process(ctx, registry)
    assert outcome.exit_code == 1
    row = spawn_store.get_spawn(ctx.runtime_root, outcome.primary_spawn_id)
    assert row.run_boundary.status == "mismatch"
    facts = [
        json.loads(line)
        for line in (root / ".meridian" / "spawns" / row.id / "runner-lifecycle.jsonl")
        .read_text()
        .splitlines()
    ]
    assert [fact["reason"] for fact in facts if fact["event"] == "entry_mismatch"] == [
        "fork_reused_source"
    ]
    assert not any(
        json.loads(line).get("harness_session_id") == source_id
        for line in at_exec.read_text().splitlines()
    )


def test_primary_later_switch_has_one_conflict(tmp_path, monkeypatch):
    from structlog.testing import capture_logs

    from meridian.lib.launch.process.primary_attach import PrimaryAttachOutcome

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "meridian-home"))
    stub_bundle_request_and_resolve(monkeypatch, model="openai/test-model", harness=HarnessId.CODEX)
    prepend_fake_executables(monkeypatch, tmp_path, "codex")
    (tmp_path / "mars.toml").write_text('[settings]\ntargets = [".codex"]\n')
    registry = HarnessRegistry.with_defaults()
    ctx = build_launch_context(
        spawn_id="preview",
        request=SpawnRequest(harness="codex", model="openai/test-model", prompt="hello"),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.PRIMARY,
            runtime_root=str(tmp_path / ".meridian"),
            project_paths_project_root=str(tmp_path),
            project_paths_execution_cwd=str(tmp_path),
        ),
        harness_registry=registry,
        dry_run=True,
    )

    def attach(
        harness_id,
        spawn_id,
        log_dir,
        control_root,
        task_cwd,
        env,
        spec,
        launcher,
        on_running,
        observer,
    ):
        observer("entry-thread")
        on_running(1234)
        observer("later-thread")
        return PrimaryAttachOutcome(exit_code=0, session_id="later-thread", tui_pid=1234)

    with capture_logs() as logs:
        outcome = run_harness_process(ctx, registry, run_primary_attach_fn=attach)
    assert outcome.exit_code == 0
    assert outcome.resolved_harness_session_id == "entry-thread"
    assert len([log for log in logs if log["event"] == "native_binding_conflict"]) == 1
