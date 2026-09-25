# qa-validated: test-suite-redesign
"""Claude session seeding, repair, and resume tests.

Verifies that fresh Claude primary launches seed a --session-id, that
planned session IDs bind before exec and conflicting observations
never change the conversation, and that resume launches do not inject seed args.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from meridian.lib.config.settings import load_config
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.claude import project_slug
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.context import build_launch_context
from meridian.lib.launch.process.runner import run_harness_process
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SessionRequest,
    SpawnRequest,
)
from meridian.lib.launch.types import SessionMode
from meridian.lib.state import session_store
from meridian.lib.state.spawn_store import list_spawns
from tests.support.launch import stub_bundle_request_and_resolve


def _write_minimal_mars_config(project_root: Path) -> None:
    (project_root / "mars.toml").write_text(
        '[settings]\ntargets = [".claude"]\n',
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _stub_launch_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_bundle_request_and_resolve(
        monkeypatch,
        model="claude-sonnet-4-5",
        harness=HarnessId.CLAUDE,
    )


def _build_primary_launch_context(
    *,
    project_root: Path,
    harness_id: HarnessId,
    model: str,
    prompt: str = "primary prompt",
    extra_args: tuple[str, ...] = (),
    session: SessionRequest | None = None,
) -> tuple[Any, Any]:
    _write_minimal_mars_config(project_root)
    harness_registry = get_default_harness_registry()
    config = load_config(project_root)
    launch_context = build_launch_context(
        spawn_id=f"dry-run-primary-{harness_id.value}",
        request=SpawnRequest(
            prompt=prompt,
            prompt_is_composed=False,
            model=model,
            harness=harness_id.value,
            extra_args=extra_args,
            session=session or SessionRequest(),
            launch_policy_snapshot=(
                LaunchPolicySnapshot(model=model, harness=harness_id.value)
                if session is not None else None
            ),
        ),
        runtime=LaunchRuntime(
            argv_intent=LaunchArgvIntent.REQUIRED,
            composition_surface=LaunchCompositionSurface.PRIMARY,
            config_snapshot=config.model_dump(mode="json", exclude_none=True),
            runtime_root=(project_root / ".meridian").as_posix(),
            project_paths_project_root=project_root.as_posix(),
            project_paths_execution_cwd=project_root.as_posix(),
        ),
        harness_registry=harness_registry,
        dry_run=True,
    )
    return launch_context, harness_registry


def _no_observed_session(**kwargs: object) -> None:
    _ = kwargs
    return None


@pytest.mark.slow
def test_run_harness_process_fresh_claude_primary_seeds_session_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Fresh Claude primary launch seeds --session-id for all launches."""
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "seed-reuse"
    project_root.mkdir()
    launch_context, harness_registry = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.CLAUDE,
        model="claude-sonnet-4-5",
    )
    claude_adapter = harness_registry.get_subprocess_harness(HarnessId.CLAUDE)
    captured: dict[str, object] = {}

    def fake_run_primary_process_with_capture(
        command: Any,
        cwd: Any,
        env: Any,
        output_log_path: Any,
        on_child_started: Any = None,
    ) -> tuple[int, int]:
        command = tuple(command)
        captured["command"] = command
        if "--session-id" in command:
            idx = command.index("--session-id")
            captured["command_session_id"] = command[idx + 1]
        assert callable(on_child_started)
        on_child_started(555)
        return (0, 555)

    monkeypatch.setattr(claude_adapter, "observe_session_id", _no_observed_session)

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
        stop_session_fn=lambda *args, **kwargs: None,
        update_session_harness_id_fn=lambda *args, **kwargs: None,
    )

    # No pre-seeded session from the launch context; Claude generates one in the command.
    assert launch_context.seed_harness_session_id in (None, "")
    assert "command_session_id" in captured
    seeded_id = captured["command_session_id"]
    assert outcome.resolved_harness_session_id == seeded_id
    spawns = list_spawns(launch_context.runtime_root)
    assert len(spawns.records) == 1
    assert spawns.records[0].harness_session_id == seeded_id
    session = session_store.get_session_record(launch_context.runtime_root, outcome.chat_id)
    assert session is not None
    assert session.spawn_id == spawns.records[0].id
    assert session.harness_session_id == seeded_id


@pytest.mark.slow
def test_run_harness_process_keeps_binding_when_observed_session_differs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A differing post-exit observation cannot repoint the entry chat."""
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "seed-repair"
    project_root.mkdir()
    launch_context, harness_registry = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.CLAUDE,
        model="claude-sonnet-4-5",
    )
    claude_adapter = harness_registry.get_subprocess_harness(HarnessId.CLAUDE)
    observed_id = "observed-different-session"

    def fake_run_primary_process_with_capture(
        command: Any,
        cwd: Any,
        env: Any,
        output_log_path: Any,
        on_child_started: Any = None,
    ) -> tuple[int, int]:
        assert callable(on_child_started)
        on_child_started(666)
        return (0, 666)

    def observed_session(**kwargs: object) -> str:
        _ = kwargs
        return observed_id

    monkeypatch.setattr(claude_adapter, "observe_session_id", observed_session)

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
        stop_session_fn=lambda *args, **kwargs: None,
        update_session_harness_id_fn=lambda *args, **kwargs: None,
    )

    assert outcome.exit_code == 1
    assert outcome.resolved_harness_session_id != observed_id
    assert outcome.resolved_harness_session_id
    spawns = list_spawns(launch_context.runtime_root)
    assert spawns.records[0].terminal is not None
    assert spawns.records[0].terminal.error == "entry_mismatch"
    assert str(spawns.records[0].status) == "failed"
    assert all(
        spawn.harness_session_id == outcome.resolved_harness_session_id for spawn in spawns.records
    )


@pytest.mark.slow
def test_run_harness_process_reconciles_claude_tui_trampoline_session_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", fake_home.as_posix())
    project_root = tmp_path / "claude-tui-trampoline"
    project_root.mkdir()
    launch_context, harness_registry = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.CLAUDE,
        model="claude-sonnet-4-5",
    )
    real_session_id = "9a4846b0-5380-461d-98cb-304e7cee6e64"

    def fake_run_primary_process_with_capture(
        command: Any,
        cwd: Any,
        env: Any,
        output_log_path: Any,
        on_child_started: Any = None,
    ) -> tuple[int, int]:
        command = tuple(command)
        recorded_session_id = command[command.index("--session-id") + 1]
        project_dir = fake_home / ".claude" / "projects" / project_slug(project_root)
        project_dir.mkdir(parents=True)
        (project_dir / f"{real_session_id}.jsonl").write_text(
            "\n".join(
                (
                    json.dumps({"type": "agent-setting", "sessionId": real_session_id}),
                    json.dumps(
                        {
                            "type": "user",
                            "message": {"role": "user", "content": "real prompt"},
                            "timestamp": 1781827539538,
                            "sessionId": real_session_id,
                        }
                    ),
                )
            )
            + "\n",
            encoding="utf-8",
        )
        (fake_home / ".claude" / "history.jsonl").write_text(
            "\n".join(
                (
                    json.dumps(
                        {
                            "display": "/tui fullscreen",
                            "project": project_root.as_posix(),
                            "sessionId": recorded_session_id,
                            "timestamp": 1781827479996,
                        }
                    ),
                    json.dumps(
                        {
                            "display": "real prompt",
                            "project": project_root.as_posix(),
                            "sessionId": real_session_id,
                            "timestamp": 1781827539538,
                        }
                    ),
                )
            )
            + "\n",
            encoding="utf-8",
        )
        assert callable(on_child_started)
        on_child_started(667)
        return (0, 667)

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
    )

    assert outcome.resolved_harness_session_id != real_session_id
    assert outcome.resolved_harness_session_id
    assert outcome.chat_id is not None
    spawns = list_spawns(launch_context.runtime_root)
    assert len(spawns.records) == 1
    assert spawns.records[0].trampoline_successor_id == real_session_id
    assert spawns.records[0].harness_session_id == outcome.resolved_harness_session_id
    assert (
        session_store.get_session_harness_id(launch_context.runtime_root, outcome.chat_id)
        == outcome.resolved_harness_session_id
    )


@pytest.mark.slow
def test_run_harness_process_resume_does_not_inject_seed_args(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Resume launches must not inject seed session args into passthrough."""
    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    project_root = tmp_path / "seed-resume"
    project_root.mkdir()
    launch_context, _harness_registry = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.CLAUDE,
        model="claude-sonnet-4-5",
        session=SessionRequest(
            requested_harness_session_id="existing-session-id",
            continue_chat_id="c42",
            primary_session_mode=SessionMode.RESUME.value,
        ),
    )
    # Resume path: adapter returns the existing session ID, no session_args injection
    assert launch_context.seed_harness_session_args == ()
    assert launch_context.seed_harness_session_id == "existing-session-id"
    plan = launch_context.binding.spec.native_identity_plan
    assert plan.operation == "resume"
    assert plan.harness_session_id == "existing-session-id"


def test_primary_claude_exec_receives_prebound_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import shlex

    from tests.support.executables import prepend_fake_executables

    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    prepend_fake_executables(monkeypatch, tmp_path, "claude")
    root = tmp_path / "repo"
    root.mkdir()
    context, registry = _build_primary_launch_context(
        project_root=root, harness_id=HarnessId.CLAUDE, model="claude-sonnet-4-5",
    )
    argv_log = tmp_path / "argv"
    binding_log = tmp_path / "binding-at-exec.jsonl"
    shim = tmp_path / "fake-bin" / "claude"
    shim.write_text(
        "#!/bin/sh\n"
        f"cp {shlex.quote(str(context.runtime_root / 'sessions.jsonl'))} "
        f"{shlex.quote(str(binding_log))}\n"
        f"printf '%s\\n' \"$@\" > {shlex.quote(str(argv_log))}\n"
        "exit 0\n"
    )
    outcome = run_harness_process(context, registry)
    assert outcome.exit_code == 0
    argv = argv_log.read_text().splitlines()
    native_id = argv[argv.index("--session-id") + 1]
    events = [json.loads(line) for line in binding_log.read_text().splitlines()]
    assert any(row.get("harness_session_id") == native_id for row in events)
    assert outcome.chat_id is not None
    record = session_store.get_session_record(context.runtime_root, outcome.chat_id)
    assert record is not None
    assert record.harness_session_id == native_id == outcome.resolved_harness_session_id
    assert record.native_store == str(
        tmp_path / "home" / ".claude" / "projects" / project_slug(root)
    )


def test_claude_fork_plan_waits_for_owned_new_identity(tmp_path: Path) -> None:
    context, _ = _build_primary_launch_context(
        project_root=tmp_path, harness_id=HarnessId.CLAUDE, model="claude-sonnet-4-5",
        session=SessionRequest(
            requested_harness_session_id="source-native", continue_fork=True,
            primary_session_mode=SessionMode.FORK.value,
        ),
    )
    plan = context.binding.spec.native_identity_plan
    assert plan.operation == "fork"
    assert plan.harness_session_id is None
    assert plan.native_store
    assert "--session-id" not in context.binding.argv
    assert "--fork-session" in context.binding.argv
    assert context.binding.argv[context.binding.argv.index("--resume") + 1] == "source-native"


@pytest.mark.parametrize("existing_exit", [False, True])
def test_claude_trampoline_exit_uses_own_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing_exit: bool,
) -> None:
    import shlex
    import subprocess
    import sys
    from dataclasses import replace

    from meridian.lib.ops.session_target import resolve_session_log_target
    from meridian.lib.state.paths import resolve_project_runtime_root_for_write
    from tests.support.executables import prepend_fake_executables

    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "meridian-home"))
    prepend_fake_executables(monkeypatch, tmp_path, "claude")
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", str(root))
    monkeypatch.setenv("MERIDIAN_TASK_DIR", str(root))
    context, registry = _build_primary_launch_context(
        project_root=root, harness_id=HarnessId.CLAUDE, model="claude-sonnet-4-5",
    )
    context = replace(context, runtime_root=resolve_project_runtime_root_for_write(root))
    store = tmp_path / "home" / ".claude" / "projects" / project_slug(root)
    store.mkdir(parents=True)
    successor = "9a4846b0-5380-461d-98cb-304e7cee6e64"
    existing_chat = None
    if existing_exit:
        existing_chat = session_store.start_session(
            context.runtime_root, "claude", successor, "claude-sonnet-4-5",
            native_store=str(store),
        )
        session_store.stop_session(context.runtime_root, existing_chat)
    # Match Claude's native history shape; the actual child supplies the assigned entry ID.
    history = "\n".join(json.dumps(row) for row in (
        {"display": "/tui fullscreen", "project": str(root), "sessionId": "%s",
         "timestamp": 1781827479996},
        {"display": "TRAMPOLINE-EXIT", "project": str(root), "sessionId": successor,
         "timestamp": 1781827539538},
    )) + "\n"
    transcript = json.dumps({
        "type": "user", "sessionId": successor, "timestamp": 1781827539538,
        "message": {"role": "user", "content": "TRAMPOLINE-EXIT"},
    }) + "\n"
    shim = tmp_path / "fake-bin" / "claude"
    shim.write_text(
        '#!/bin/sh\nwhile [ "$#" -gt 0 ]; do\n'
        ' if [ "$1" = "--session-id" ]; then shift; entry=$1; fi\n shift\ndone\n'
        f"printf {shlex.quote(history)} \"$entry\" > "
        f"{shlex.quote(str(store.parent.parent / 'history.jsonl'))}\n"
        f"printf '%s' {shlex.quote(transcript)} > "
        f"{shlex.quote(str(store / f'{successor}.jsonl'))}\n"
    )
    outcome = run_harness_process(context, registry)
    assert outcome.exit_code == 0
    row = list_spawns(context.runtime_root).records[0]
    entry = session_store.get_session_record(context.runtime_root, outcome.chat_id)
    assert entry is not None and entry.harness_session_id != successor
    assert row.harness_session_id == entry.harness_session_id == outcome.resolved_harness_session_id
    assert row.trampoline_successor_id == successor
    assert row.entry_chat_id == entry.chat_id
    assert row.exit_identity == "verified"
    assert row.exit_chat_id and row.exit_chat_id != entry.chat_id
    exit_chat = session_store.get_session_record(context.runtime_root, row.exit_chat_id)
    assert exit_chat is not None and exit_chat.harness_session_id == successor
    assert exit_chat.native_store == entry.native_store == str(store)
    if existing_exit:
        assert row.exit_chat_id == existing_chat
    target = resolve_session_log_target(
        ref=row.id, file_path=None, project_root=root, runtime_root=context.runtime_root,
    )
    assert target.session_id == successor
    shown = subprocess.run(
        [sys.executable, "-m", "meridian", "spawn", "show", row.id],
        cwd=root, text=True, capture_output=True, timeout=15,
    )
    assert shown.returncode == 0, shown.stderr
    assert f"entry {entry.chat_id} ({entry.harness_session_id})" in shown.stdout
    assert f"→ exit {exit_chat.chat_id} ({successor})" in shown.stdout
