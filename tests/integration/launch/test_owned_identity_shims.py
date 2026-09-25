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


@pytest.mark.parametrize("harness", [HarnessId.CODEX, HarnessId.OPENCODE])
def test_owned_event_pins_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: HarnessId
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
        harness_id, spawn_id, log_dir, control_root, task_cwd, env, spec, launcher, on_running
    ):
        # Replace the external backend/TUI transport, not identity parsing or persistence.
        completed = subprocess.run(
            [str(tmp_path / "fake-bin" / str(harness_id))],
            env=env,
            cwd=task_cwd or control_root,
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(completed.stdout)
        native_id = get_harness_bundle(harness_id).extractor.detect_session_id_from_event(
            RawHarnessEvent(event_type=payload["type"], harness_id=str(harness_id), payload=payload)
        )
        return PrimaryAttachOutcome(exit_code=0, session_id=native_id, tui_pid=None)

    outcome = run_harness_process(context, registry, run_primary_attach_fn=attach)
    assert outcome.exit_code == 0
    assert outcome.chat_id
    record = session_store.get_session_record(root / ".meridian", outcome.chat_id)
    assert record and record.harness_session_id == NATIVE_ID
    assert record.native_store == str(
        tmp_path / ("codex/sessions" if harness == HarnessId.CODEX else "opencode/storage")
    )
    store = Path(record.native_store)
    native_file = (
        store / f"rollout-2026-01-01T00-00-00-{NATIVE_ID}.jsonl"
        if harness == HarnessId.CODEX
        else store / "session" / f"{NATIVE_ID}.json"
    )
    native_file.parent.mkdir(parents=True)
    native_file.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": NATIVE_ID}}) + "\n"
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("OPENCODE_HOME", str(tmp_path / "elsewhere"))
    assert (
        registry.get(harness).resolve_session_file(
            project_root=root, session_id=NATIVE_ID, config_root_hint=store
        )
        == native_file
    )
