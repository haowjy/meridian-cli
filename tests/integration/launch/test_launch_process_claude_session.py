# qa-validated: test-suite-redesign
"""Claude session seeding, repair, and resume tests.

Verifies that fresh Claude primary launches seed a --session-id, that
planned session IDs bind before exec and conflicting observations
never change the conversation, and that resume launches do not inject seed args.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from meridian.lib.config.loader import load_config
from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
from meridian.lib.core.native_identity import NativeSessionUnavailable
from meridian.lib.core.types import HarnessId
from meridian.lib.harness.attempt_facts import AttemptFacts
from meridian.lib.harness.claude import project_slug
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.process.runner import (
    PrimaryExecutionResult,
    PrimaryProcessResult,
    run_harness_process,
)
from meridian.lib.launch.request import (
    LaunchArgvIntent,
    LaunchCompositionSurface,
    LaunchRuntime,
    SessionRequest,
    SpawnRequest,
)
from meridian.lib.launch.types import SessionMode
from meridian.lib.platform.launch_gate import ProcessIdentity
from meridian.lib.state import session_store
from meridian.lib.state.spawn_store import list_spawns
from tests.support.claude_boundary_process import ENTRY_SESSION_TOKEN
from tests.support.launch import build_launch_context, stub_bundle_request_and_resolve


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
                if session is not None
                else None
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
    harness_registry.get_subprocess_harness(HarnessId.CLAUDE)
    captured: dict[str, object] = {}

    def fake_run_primary_process_with_capture(
        command: Any,
        cwd: Any,
        env: Any,
        output_log_path: Any,
        process_launcher: Any,
        on_child_started: Any,
        launcher_diagnostics_path: Any,
    ) -> tuple[int, int, None]:
        _ = launcher_diagnostics_path
        command = tuple(command)
        captured["command"] = command
        if "--session-id" in command:
            idx = command.index("--session-id")
            captured["command_session_id"] = command[idx + 1]
        assert callable(on_child_started)
        on_child_started(ProcessIdentity(555, 100.0, 555))
        return PrimaryProcessResult(0, 555, None)

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        prepared=launch_context.prepared,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
    )

    # No pre-seeded session from the launch context; Claude generates one in the command.
    assert launch_context.seed_harness_session_id in (None, "")
    assert "command_session_id" in captured
    seeded_id = captured["command_session_id"]
    assert outcome.resolved_harness_session_id == seeded_id
    spawns = list_spawns(launch_context.runtime_root)
    assert len(spawns.records) == 1
    assert spawns.records[0].harness_session_id == seeded_id
    session = session_store.chat_record(launch_context.runtime_root, outcome.chat_id)
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
    observed_id = "observed-different-session"

    def fake_run_primary_process_with_capture(
        command: Any,
        cwd: Any,
        env: Any,
        output_log_path: Any,
        process_launcher: Any,
        on_child_started: Any,
        launcher_diagnostics_path: Any,
    ) -> tuple[int, int, None]:
        _ = command, cwd, env, output_log_path, launcher_diagnostics_path
        assert callable(on_child_started)
        on_child_started(ProcessIdentity(666, 100.0, 666))
        return PrimaryProcessResult(0, 666, None)

    def capture_facts(*args: object, **kwargs: object):
        return PrimaryExecutionResult(
            exit_code=0,
            managed_session_id=None,
            cancelled=False,
            facts=AttemptFacts(first_session_id=observed_id),
            launcher_diagnostics_path=None,
        )

    monkeypatch.setattr(
        "meridian.lib.launch.process.runner._execute_primary_process", capture_facts
    )

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        prepared=launch_context.prepared,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
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
        process_launcher: Any,
        on_child_started: Any,
        launcher_diagnostics_path: Any,
    ) -> tuple[int, int, None]:
        _ = cwd, env, output_log_path, launcher_diagnostics_path
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
        on_child_started(ProcessIdentity(667, 100.0, 667))
        return PrimaryProcessResult(0, 667, None)

    outcome = run_harness_process(
        launch_context,
        harness_registry,
        prepared=launch_context.prepared,
        run_primary_process_with_capture_fn=fake_run_primary_process_with_capture,
    )

    assert outcome.resolved_harness_session_id != real_session_id
    assert outcome.resolved_harness_session_id
    assert outcome.chat_id is not None
    spawns = list_spawns(launch_context.runtime_root)
    assert len(spawns.records) == 1
    assert spawns.records[0].run_boundary.trampoline_successor_id == real_session_id
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
    source = tmp_path / "source"
    source.mkdir()
    (source / "existing-session-id.jsonl").write_text(
        json.dumps({"sessionId": "existing-session-id"}) + "\n"
    )
    project_root = tmp_path / "seed-resume"
    project_root.mkdir()
    launch_context, _harness_registry = _build_primary_launch_context(
        project_root=project_root,
        harness_id=HarnessId.CLAUDE,
        model="claude-sonnet-4-5",
        session=SessionRequest(
            requested_harness_session_id="existing-session-id",
            source_native_store=str(source),
            continue_chat_id="c42",
            primary_session_mode=SessionMode.RESUME.value,
        ),
    )
    # Resume path: adapter returns the existing session ID, no session_args injection
    assert launch_context.seed_harness_session_args == ()
    assert launch_context.seed_harness_session_id == "existing-session-id"
    plan = launch_context.binding.spec.native_identity
    assert plan.operation == "resume"
    assert plan.session_id == "existing-session-id"


@pytest.mark.parametrize("signal", ["silent", "match", "mismatch", "switch"])
def test_primary_claude_exec_receives_prebound_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signal: str,
) -> None:
    import shlex

    from tests.support.executables import prepend_fake_executables

    monkeypatch.delenv("MERIDIAN_CHAT_ID", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    prepend_fake_executables(monkeypatch, tmp_path, "claude")
    root = tmp_path / "repo"
    root.mkdir()
    context, registry = _build_primary_launch_context(
        project_root=root,
        harness_id=HarnessId.CLAUDE,
        model="claude-sonnet-4-5",
        extra_args=("--print",),
    )
    argv_log = tmp_path / "argv"
    binding_log = tmp_path / "binding-at-exec.jsonl"
    shim = tmp_path / "fake-bin" / "claude"
    frames = ""
    if signal != "silent":
        frames = (
            'while [ "$#" -gt 0 ]; do\n'
            ' if [ "$1" = "--session-id" ]; then shift; id=$1; fi\n shift\ndone\n'
            + ("id=wrong-entry\n" if signal == "mismatch" else "")
            + 'printf \'{"type":"system","subtype":"init",'
            '"session_id":"%s"}\\n\' "$id"\n'
        )
    if signal == "switch":
        frames += 'printf \'%s\\n\' \'{"type":"result","session_id":"later-id"}\'\n'
    shim.write_text(
        "#!/bin/sh\n"
        f"cp {shlex.quote(str(context.runtime_root / 'sessions.jsonl'))} "
        f"{shlex.quote(str(binding_log))}\n"
        f"printf '%s\\n' \"$@\" > {shlex.quote(str(argv_log))}\n" + frames + "exit 0\n"
    )
    outcome = run_harness_process(context, registry, prepared=context.prepared)
    assert outcome.exit_code == (1 if signal == "mismatch" else 0)
    argv = argv_log.read_text().splitlines()
    native_id = argv[argv.index("--session-id") + 1]
    events = [json.loads(line) for line in binding_log.read_text().splitlines()]
    assert any(row.get("harness_session_id") == native_id for row in events)
    # The entry generation owns its final policy before native execution.
    assert any(row.get("launch_policy_snapshot") for row in events)
    assert outcome.chat_id is not None
    record = session_store.chat_record(context.runtime_root, outcome.chat_id)
    assert record is not None
    spawn_row = list_spawns(context.runtime_root).records[0]
    assert record.launch_policy_snapshot == spawn_row.launch_policy_snapshot is not None
    assert record.harness_session_id == native_id == outcome.resolved_harness_session_id
    assert record.native_store == str(
        tmp_path / "home" / ".claude" / "projects" / project_slug(root)
    )

    if signal == "mismatch":
        row = list_spawns(context.runtime_root).records[0]
        assert row.status == "failed" and row.terminal is not None
        assert row.terminal.error == "entry_mismatch"
        assert row.run_boundary is not None and row.current_chat_id is None
        session_events = [
            json.loads(line)
            for line in (context.runtime_root / "sessions.jsonl").read_text().splitlines()
        ]
        assert not any(event.get("kind") == "invocation_started" for event in session_events)


def test_claude_fork_plan_waits_for_owned_new_identity(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "source-native.jsonl").write_text(json.dumps({"sessionId": "source-native"}) + "\n")
    context, _ = _build_primary_launch_context(
        project_root=tmp_path,
        harness_id=HarnessId.CLAUDE,
        model="claude-sonnet-4-5",
        session=SessionRequest(
            requested_harness_session_id="source-native",
            continue_fork=True,
            source_native_store=str(source),
            primary_session_mode=SessionMode.FORK.value,
        ),
    )
    plan = context.binding.spec.native_identity
    assert plan.operation == "fork"
    assert plan.session_id is None
    assert plan.native_store
    assert "--session-id" not in context.binding.argv
    assert "--fork-session" in context.binding.argv
    assert context.binding.argv[context.binding.argv.index("--resume") + 1] == "source-native"


@pytest.mark.parametrize("existing_exit", [False, True])
def test_unrelated_claude_trampoline_candidate_is_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing_exit: bool,
) -> None:
    import shlex
    import subprocess
    import sys
    from dataclasses import replace

    from meridian.lib.ops.session_target import resolve_transcript_source
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
        project_root=root,
        harness_id=HarnessId.CLAUDE,
        model="claude-sonnet-4-5",
    )
    context = replace(context, runtime_root=resolve_project_runtime_root_for_write(root))
    store = tmp_path / "home" / ".claude" / "projects" / project_slug(root)
    store.mkdir(parents=True)
    successor = "9a4846b0-5380-461d-98cb-304e7cee6e64"
    existing_chat = None
    if existing_exit:
        existing_chat = session_store.start_session(
            context.runtime_root,
            "claude",
            successor,
            "claude-sonnet-4-5",
            native_store=str(store),
        ).chat_id
        session_store.stop_session(context.runtime_root, existing_chat)
    # Match Claude's native history shape; the actual child supplies the assigned entry ID.
    history = (
        "\n".join(
            json.dumps(row)
            for row in (
                {
                    "display": "/tui fullscreen",
                    "project": str(root),
                    "sessionId": "%s",
                    "timestamp": 1781827479996,
                },
                {
                    "display": "TRAMPOLINE-EXIT",
                    "project": str(root),
                    "sessionId": successor,
                    "timestamp": 1781827539538,
                },
            )
        )
        + "\n"
    )
    transcript = (
        json.dumps(
            {
                "type": "user",
                "sessionId": successor,
                "timestamp": 1781827539538,
                "message": {"role": "user", "content": "TRAMPOLINE-EXIT"},
            }
        )
        + "\n"
    )
    shim = tmp_path / "fake-bin" / "claude"
    shim.write_text(
        '#!/bin/sh\nwhile [ "$#" -gt 0 ]; do\n'
        ' if [ "$1" = "--session-id" ]; then shift; entry=$1; fi\n shift\ndone\n'
        f'printf {shlex.quote(history)} "$entry" > '
        f"{shlex.quote(str(store.parent.parent / 'history.jsonl'))}\n"
        f"printf '%s' {shlex.quote(transcript)} > "
        f"{shlex.quote(str(store / f'{successor}.jsonl'))}\nexit 1\n"
    )
    outcome = run_harness_process(context, registry, prepared=context.prepared)
    assert outcome.exit_code == 1
    row = list_spawns(context.runtime_root).records[0]
    entry = session_store.chat_record(context.runtime_root, outcome.chat_id)
    assert entry is not None and entry.harness_session_id != successor
    assert row.harness_session_id == entry.harness_session_id == outcome.resolved_harness_session_id
    assert row.run_boundary.trampoline_successor_id == successor
    assert row.chat_id == entry.chat_id
    assert (row.run_boundary.status if row.run_boundary else None) == "unresolved"
    assert row.current_chat_id is None
    with pytest.raises(NativeSessionUnavailable, match="native_transcript_missing"):
        resolve_transcript_source(
            ref=row.id,
            file_path=None,
            project_root=root,
            runtime_root=context.runtime_root,
        )
    shown = subprocess.run(
        [sys.executable, "-m", "meridian", "spawn", "show", row.id],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert shown.returncode == 0, shown.stderr
    assert f"entry {entry.chat_id} ({entry.harness_session_id})" in shown.stdout
    assert "warning: Possible session change detected" in shown.stdout
    assert successor in shown.stdout
    assert "exit-session identity unverified" in shown.stdout
    logged = subprocess.run(
        [sys.executable, "-m", "meridian", "session", "log", row.id],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert "TRAMPOLINE-EXIT" not in logged.stdout


@pytest.mark.parametrize(
    ("fork", "initial_id", "store_suffix", "status"),
    [
        pytest.param(False, ENTRY_SESSION_TOKEN, "", "unresolved", id="create-switches"),
        pytest.param(True, "fork-native", "", "unresolved", id="fork-switches"),
        pytest.param(True, "source-native", "", "mismatch", id="fork-reuses-source"),
        pytest.param(True, "fork-native", "foreign", "mismatch", id="fork-wrong-store"),
    ],
)
def test_owned_claude_observations_publish_current_chat_while_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fork: bool,
    initial_id: str,
    store_suffix: str,
    status: str,
) -> None:
    """Keep expensive launches for wiring/switches and the two fork-binding guards."""
    from dataclasses import replace

    from meridian.lib.ops.session_target import resolve_transcript_source
    from meridian.lib.state.paths import resolve_project_runtime_root_for_write
    from tests.support.executables import prepend_fake_executables

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path / "meridian-home"))
    prepend_fake_executables(monkeypatch, tmp_path, "claude")
    fixture_source = Path(__file__).parents[2] / "support" / "claude_boundary_process.py"
    fixture_executable = tmp_path / "fake-bin" / "claude"
    shutil.copyfile(fixture_source, fixture_executable)
    fixture_executable.chmod(0o755)

    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setenv("MERIDIAN_PROJECT_DIR", str(root))
    monkeypatch.setenv("MERIDIAN_TASK_DIR", str(root))
    store = tmp_path / "home" / ".claude/projects" / project_slug(root)
    session = None
    source_session_id = "source-native"
    if fork:
        store.mkdir(parents=True)
        (store / f"{source_session_id}.jsonl").write_text(
            json.dumps({"sessionId": source_session_id}) + "\n"
        )
        session = SessionRequest(
            requested_harness_session_id=source_session_id,
            continue_fork=True,
            source_native_store=str(store),
            primary_session_mode=SessionMode.FORK.value,
        )
    context, registry = _build_primary_launch_context(
        project_root=root,
        harness_id=HarnessId.CLAUDE,
        model="claude-sonnet-4-5",
        session=session,
    )
    context = replace(context, runtime_root=resolve_project_runtime_root_for_write(root))
    fixture_config = tmp_path / "claude-boundary-fixture.json"
    final_id = initial_id if fork or status == "mismatch" else "unpersisted"
    steps = (
        # Missing startup must not turn a later /resume into an entry mismatch.
        ("SessionStart", initial_id if fork else "resumed", "fork" if fork else "resume"),
        ("SessionStart", "cleared", "clear"),
        ("SessionStart", final_id, "resume" if fork else "clear"),
    )
    events = [
        {
            "hook_event_name": event,
            "session_id": sid,
            "transcript_path": str(store / store_suffix / f"{sid}.jsonl"),
            "source" if event == "SessionStart" else "reason": reason,
            "write_transcript": sid != "unpersisted",
        }
        for event, sid, reason in steps
    ]
    release_file = tmp_path / "release-child"
    fixture_config.write_text(json.dumps({"events": events, "wait_file": str(release_file)}))
    monkeypatch.setenv("_MERIDIAN_CLAUDE_BOUNDARY_FIXTURE_CONFIG", str(fixture_config))

    publication_failures = 0
    committed_switches = []
    if not fork and status == "unresolved":
        from meridian.lib.state.spawn import repository

        original_write = repository.atomic_write_text

        def fail_final_pointer(path, text):
            nonlocal publication_failures
            current_id = json.loads(text).get("current_chat_id")
            current = (
                session_store.chat_record(context.runtime_root, current_id)
                if current_id
                else None
            )
            sid = current.harness_session_id if current is not None else None
            if sid == "unpersisted" and publication_failures == 0:
                publication_failures += 1
                raise OSError("injected pointer publication interruption")
            original_write(path, text)
            if sid and (not committed_switches or committed_switches[-1] != sid):
                committed_switches.append(sid)

        monkeypatch.setattr(repository, "atomic_write_text", fail_final_pointer)

    outcomes = []
    launch = threading.Thread(
        target=lambda: outcomes.append(
            run_harness_process(context, registry, prepared=context.prepared)
        )
    )
    launch.start()
    deadline = time.monotonic() + 8
    row = None
    while time.monotonic() < deadline:
        rows = list_spawns(context.runtime_root).records
        row = rows[0] if rows else None
        cleared_live = any(
            record.harness_session_id == "cleared"
            for record in session_store.list_all_session_records(context.runtime_root)
        )
        current = (
            session_store.chat_record(context.runtime_root, row.current_chat_id)
            if row is not None and row.current_chat_id is not None
            else None
        )
        if row is not None and (
            status == "mismatch"
            or (
                cleared_live
                and current is not None
                and current.harness_session_id
                == final_id.replace(ENTRY_SESSION_TOKEN, row.harness_session_id or "")
            )
        ):
            break
        time.sleep(0.02)
    try:
        assert row is not None
        if status == "unresolved":
            # Fork returns A -> B -> A; create switches without a startup hook.
            # Both publish while the native process remains alive.
            current = session_store.chat_record(context.runtime_root, row.current_chat_id)
            assert current is not None and current.harness_session_id == final_id.replace(
                ENTRY_SESSION_TOKEN, row.harness_session_id or ""
            )
            if not fork:
                assert publication_failures == 1
    finally:
        release_file.touch()
        launch.join(timeout=10)
    assert not launch.is_alive()
    assert not any(
        thread.name.startswith("meridian-native-observer-") for thread in threading.enumerate()
    )
    outcome = outcomes[0]
    row = list_spawns(context.runtime_root).records[0]
    if not fork and status == "unresolved":
        assert committed_switches == ["resumed", "cleared", "unpersisted"]
    assert row.run_boundary is not None
    entry = session_store.chat_record(context.runtime_root, outcome.chat_id)
    assert entry is not None
    assert row.harness_session_id == entry.harness_session_id
    assert row.chat_id == entry.chat_id
    assert row.run_boundary.status == status
    assert outcome.exit_code == (1 if status == "mismatch" else 0)
    if status == "unresolved":
        assert entry.harness_session_id is not None
        assert entry.harness_session_id not in {"cleared", "resumed", source_session_id}
        if fork:
            assert entry.harness_session_id == initial_id
        current = session_store.chat_record(context.runtime_root, row.continue_chat_id)
        assert current is not None
        if not fork:
            assert current.harness_session_id == "unpersisted"
            with pytest.raises(NativeSessionUnavailable, match="native_transcript_missing"):
                resolve_transcript_source(
                    ref=row.id,
                    file_path=None,
                    project_root=root,
                    runtime_root=context.runtime_root,
                )
        else:
            assert row.continue_chat_id == entry.chat_id
        observed = [
            record
            for record in session_store.list_all_session_records(context.runtime_root)
            if record.harness_session_id == "cleared"
        ]
        assert len(observed) == 1
    else:
        assert entry.harness_session_id is None
        assert row.continue_chat_id == entry.chat_id
    if fork:
        assert (store / f"{source_session_id}.jsonl").read_text() == (
            json.dumps({"sessionId": source_session_id}) + "\n"
        )


def test_identity_error_before_spawn_allocation_does_not_assert(tmp_path, monkeypatch):
    from meridian.lib.core.lifecycle import SpawnLifecycleService
    from meridian.lib.core.native_identity import NativeEntryMismatch, NativeKeyFields

    context, registry = _build_primary_launch_context(
        project_root=tmp_path, harness_id=HarnessId.CLAUDE, model="claude-sonnet-4-5"
    )

    def refuse(*args, **kwargs):
        raise NativeEntryMismatch(
            NativeKeyFields(session_id="first"), NativeKeyFields(session_id="other")
        )

    monkeypatch.setattr(SpawnLifecycleService, "start", refuse)
    outcome = run_harness_process(context, registry, prepared=context.prepared)
    assert outcome.exit_code == 1
    assert outcome.primary_spawn_id is None
