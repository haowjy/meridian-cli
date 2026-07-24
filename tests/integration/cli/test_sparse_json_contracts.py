"""CLI contracts for commands whose JSON output has a curated projection."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from meridian.lib.ops.work_lifecycle import WorkStartInput, work_start_sync
from meridian.lib.state import spawn_store
from meridian.lib.state.paths import resolve_project_runtime_root_for_write
from tests.conftest import posix_only
from tests.support.executables import prepend_fake_executables

_NOISY_KEYS = {"stdout", "stderr", "harness_session_id", "report_body"}


@pytest.fixture
def cli_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Path, dict[str, str], Path]]:
    project_root = tmp_path / "project"
    (project_root / ".meridian").mkdir(parents=True)
    (project_root / ".meridian" / "id").write_text("sparse-json-test\n", encoding="utf-8")
    (project_root / "meridian.toml").write_text(
        "[spawn]\ndeny_headless_harnesses = []\n",
        encoding="utf-8",
    )
    prepend_fake_executables(monkeypatch, tmp_path, "codex")
    monkeypatch.setenv("MERIDIAN_HOME", (tmp_path / "home").as_posix())
    env = os.environ.copy()

    runtime_root = resolve_project_runtime_root_for_write(project_root)
    runtime_root.mkdir(parents=True, exist_ok=True)
    yield project_root, env, runtime_root


def _run_json(
    project_root: Path,
    env: dict[str, str],
    *args: str,
) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, "-m", "meridian", "--format", "json", *args],
        cwd=project_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    return payload


def _assert_noisy_keys_absent(payload: object) -> None:
    if isinstance(payload, dict):
        assert _NOISY_KEYS.isdisjoint(payload)
        for value in payload.values():
            _assert_noisy_keys_absent(value)
    elif isinstance(payload, list):
        for value in payload:
            _assert_noisy_keys_absent(value)


def _seed_terminal_spawn(runtime_root: Path, project_root: Path) -> None:
    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p1",
        chat_id="c1",
        model="gpt-5.4",
        agent="implementer",
        harness="codex",
        kind="primary",
        prompt="Implement sparse JSON.",
        desc="Sparse JSON",
        work_id="sparse-json",
        harness_session_id="internal-session-id",
        task_cwd=project_root.as_posix(),
    )
    spawn_store.finalize_spawn(
        runtime_root,
        "p1",
        "succeeded",
        0,
        origin="runner",
        duration_secs=1.25,
    )
    report_path = runtime_root / "spawns" / "p1" / "report.md"
    report_path.write_text("Completed the sparse JSON projection.\n", encoding="utf-8")


def _seed_work_spawn(runtime_root: Path, project_root: Path) -> None:
    spawn_store.start_spawn(
        runtime_root,
        spawn_id="p2",
        chat_id="c2",
        model="gpt-5.4",
        agent="implementer",
        harness="codex",
        prompt="Implement sparse JSON.",
        desc="Sparse JSON",
        work_id="sparse-json",
        task_cwd=project_root.as_posix(),
    )
    spawn_store.finalize_spawn(
        runtime_root,
        "p2",
        "succeeded",
        0,
        origin="runner",
        duration_secs=1.25,
    )


@pytest.mark.integration
@posix_only
def test_spawn_create_json_contract(
    cli_project: tuple[Path, dict[str, str], Path],
) -> None:
    project_root, env, _ = cli_project
    payload = _run_json(
        project_root,
        env,
        "--harness",
        "codex",
        "spawn",
        "--agent",
        "",
        "--background",
        "--dry-run",
        "Inspect the projection.",
    )

    assert {"status", "model", "harness_id", "composed_prompt"} <= payload.keys()
    _assert_noisy_keys_absent(payload)


@pytest.mark.integration
@posix_only
def test_spawn_show_json_contract(
    cli_project: tuple[Path, dict[str, str], Path],
) -> None:
    project_root, env, runtime_root = cli_project
    _seed_terminal_spawn(runtime_root, project_root)

    payload = _run_json(project_root, env, "spawn", "show", "p1")

    assert {"spawn_id", "status", "model", "harness", "report_path", "report_summary"} <= (
        payload.keys()
    )
    _assert_noisy_keys_absent(payload)

    with_report = _run_json(project_root, env, "spawn", "show", "p1", "--report")
    assert with_report["report_body"] == "Completed the sparse JSON projection."


@pytest.mark.integration
@posix_only
def test_spawn_status_json_contract(
    cli_project: tuple[Path, dict[str, str], Path],
) -> None:
    project_root, env, runtime_root = cli_project
    _seed_terminal_spawn(runtime_root, project_root)

    payload = _run_json(project_root, env, "spawn", "status", "p1")

    assert {"spawn_id", "status", "model", "harness", "report_path", "report_summary"} <= (
        payload.keys()
    )
    _assert_noisy_keys_absent(payload)


@pytest.mark.integration
@posix_only
def test_spawn_wait_json_contract(
    cli_project: tuple[Path, dict[str, str], Path],
) -> None:
    project_root, env, runtime_root = cli_project
    _seed_terminal_spawn(runtime_root, project_root)

    payload = _run_json(project_root, env, "spawn", "wait", "p1")

    assert {
        "total_runs",
        "succeeded_runs",
        "failed_runs",
        "cancelled_runs",
        "timed_out_runs",
        "any_failed",
        "spawns",
    } <= payload.keys()
    assert {"spawn_id", "status", "model", "harness"} <= payload["spawns"][0].keys()
    _assert_noisy_keys_absent(payload)


@pytest.mark.integration
@posix_only
def test_work_show_json_contract(
    cli_project: tuple[Path, dict[str, str], Path],
) -> None:
    project_root, env, runtime_root = cli_project
    work_start_sync(
        WorkStartInput(
            label="sparse-json",
            description="Project noisy output.",
            goal="Keep the useful summary.",
            project_root=project_root.as_posix(),
        )
    )
    _seed_work_spawn(runtime_root, project_root)

    payload = _run_json(project_root, env, "work", "show", "sparse-json")

    assert {
        "name",
        "status",
        "goal",
        "description",
        "created_at",
        "work_dir",
        "spawns",
        "sessions",
    } <= payload.keys()
    assert set(payload["spawns"][0]) == {"id", "status", "model", "desc"}
    assert {"task_dir", "worktree_path", "worktree_exists", "worktree_pending"}.isdisjoint(
        payload
    )
    _assert_noisy_keys_absent(payload)


@pytest.mark.integration
@posix_only
def test_hooks_run_json_contract(
    cli_project: tuple[Path, dict[str, str], Path],
) -> None:
    project_root, env, _ = cli_project
    (project_root / "meridian.toml").write_text(
        "[[hooks]]\n"
        'name = "projection-test"\n'
        'event = "spawn.finalized"\n'
        'command = "printf hook-output; printf hook-error >&2"\n',
        encoding="utf-8",
    )

    payload = _run_json(project_root, env, "hooks", "run", "projection-test")

    assert {
        "hook",
        "event",
        "outcome",
        "success",
        "skipped",
        "skip_reason",
        "error",
        "exit_code",
        "duration_ms",
    } <= payload.keys()
    _assert_noisy_keys_absent(payload)

    with_output = _run_json(
        project_root,
        env,
        "hooks",
        "run",
        "projection-test",
        "--output",
    )
    assert with_output["stdout"] == "hook-output"
    assert with_output["stderr"] == "hook-error"
