"""Subprocess-level regression coverage for bound harness environments."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import cast

import pytest

from meridian.lib.core.types import HarnessId, ModelId, SpawnId
from meridian.lib.harness.adapter import SpawnParams
from meridian.lib.harness.claude import ClaudeAdapter
from meridian.lib.harness.connections.base import ConnectionConfig
from meridian.lib.harness.connections.claude_ws import ClaudeConnection
from meridian.lib.harness.connections.pi_rpc import PiRpcConnection
from meridian.lib.harness.pi import PiAdapter
from meridian.lib.launch.env import apply_pi_bind_time_env, build_harness_child_env
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import PermissionConfig, UnsafeNoOpPermissionResolver

_PI_HELP = (
    "--mode rpc --model --append-system-prompt --session --fork "
    "--session-dir --no-extensions --no-skills --no-context-files "
    "--no-prompt-templates -e --extension PI_CODING_AGENT_SESSION_DIR"
)


def _write_recording_harness(path: Path, *, harness: HarnessId) -> None:
    main_protocol = (
        "for line in sys.stdin:\n"
        "    payload = json.loads(line)\n"
        "    if payload.get('type') == 'prompt':\n"
        "        print(json.dumps({'type': 'agent_start'}), flush=True)\n"
        "        print(json.dumps({'type': 'agent_end', 'messages': []}), flush=True)\n"
        "    elif payload.get('type') == 'abort':\n"
        "        raise SystemExit(0)\n"
        if harness is HarnessId.PI
        else "for _line in sys.stdin:\n    pass\n"
    )
    version_output = "pi 3.0.0" if harness is HarnessId.PI else "2.1.0"
    help_branch = (
        "if '--help' in sys.argv[1:]:\n"
        f"    print({_PI_HELP!r})\n"
        "    raise SystemExit(0)\n"
        if harness is HarnessId.PI
        else ""
    )
    path.write_text(
        f"#!{sys.executable}\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "with open(os.environ['HARNESS_ENV_LOG'], 'a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps({'argv': sys.argv[1:], 'env': dict(os.environ)}) + '\\n')\n"
        "if '--version' in sys.argv[1:]:\n"
        f"    print({version_output!r})\n"
        "    raise SystemExit(0)\n"
        f"{help_branch}"
        f"{main_protocol}",
        encoding="utf-8",
    )
    path.chmod(0o755)


async def _wait_for_records(path: Path, count: int) -> list[dict[str, object]]:
    for _ in range(100):
        if path.exists():
            records = [json.loads(line) for line in path.read_text().splitlines()]
            if len(records) >= count:
                return records
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {count} harness environment records at {path}")


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable shim")
async def test_claude_probe_and_main_process_share_sanitized_bound_environment(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_recording_harness(bin_dir / "claude", harness=HarnessId.CLAUDE)
    env_log = tmp_path / "claude-env.jsonl"
    bound_home = tmp_path / "bound-home"
    child_env = build_harness_child_env(
        base_env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(bound_home),
            "HARNESS_ENV_LOG": str(env_log),
            "CLAUDECODE": "blocked-nesting-sentinel",
            "MERIDIAN_SECRET_PROBE": "should-not-reach-child",
        },
        adapter=ClaudeAdapter(),
        run_params=SpawnParams(prompt="test", model=ModelId("claude-sonnet-4-6")),
        permission_config=PermissionConfig(),
    )
    connection = ClaudeConnection()
    await connection.start(
        ConnectionConfig(
            spawn_id=SpawnId("p-env-boundary-claude"),
            harness_id=HarnessId.CLAUDE,
            prompt="hello",
            control_root=tmp_path,
            child_env=child_env,
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.CLAUDE,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )
    try:
        records = await _wait_for_records(env_log, 2)
    finally:
        await connection.stop()

    assert any("--version" in record["argv"] for record in records)  # type: ignore[operator]
    assert any("--version" not in record["argv"] for record in records)  # type: ignore[operator]
    for record in records:
        env = record["env"]
        assert isinstance(env, dict)
        assert env["PATH"] == child_env["PATH"]
        assert env["HOME"] == str(bound_home)
        assert "CLAUDECODE" not in env
        assert "MERIDIAN_SECRET_PROBE" not in env


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX executable shim")
@pytest.mark.parametrize(
    ("role", "timeout", "interval", "expected_timeout", "expected_interval"),
    [
        ("spawned", 1.5, 0.25, "1500", "250"),
        ("primary", 1.5, 0.25, None, "250"),
        ("spawned", None, None, None, None),
        ("spawned", float("nan"), float("nan"), None, None),
        ("spawned", float("inf"), float("inf"), None, None),
        ("spawned", 0.0, 0.0, None, None),
        ("spawned", -1.0, -1.0, None, None),
    ],
)
async def test_pi_main_process_receives_role_gated_bound_runtime_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    timeout: float | None,
    interval: float | None,
    expected_timeout: str | None,
    expected_interval: str | None,
) -> None:
    fake_pi = tmp_path / "pi"
    _write_recording_harness(fake_pi, harness=HarnessId.PI)
    env_log = tmp_path / "pi-env.jsonl"
    bound_home = tmp_path / "bound-home"
    monkeypatch.setenv("HOME", str(bound_home))
    child_env = build_harness_child_env(
        base_env={
            "PATH": os.environ["PATH"],
            "HOME": str(bound_home),
            "HARNESS_ENV_LOG": str(env_log),
        },
        adapter=PiAdapter(),
        run_params=SpawnParams(
            prompt="test",
            model=ModelId("openai-codex/gpt-5.4-mini"),
            interactive=role == "primary",
        ),
        permission_config=PermissionConfig(),
        runtime_env_overrides={"MERIDIAN_PI_BINARY": str(fake_pi)},
    )
    apply_pi_bind_time_env(
        child_env,
        launch_role=role,  # type: ignore[arg-type]
        timeout_seconds=timeout,
        interval_seconds=interval,
        reset_on_activity=None,
    )
    connection = PiRpcConnection()
    await connection.start(
        ConnectionConfig(
            spawn_id=SpawnId("p-env-boundary-pi"),
            harness_id=HarnessId.PI,
            prompt="hello",
            control_root=tmp_path,
            child_env=child_env,
            pi_session_role=role,  # type: ignore[arg-type]
        ),
        ResolvedLaunchSpec(
            harness=HarnessId.PI,
            prompt="hello",
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
    )
    try:
        records = await _wait_for_records(env_log, 3)
    finally:
        await connection.stop()

    main_record = next(
        record
        for record in records
        if "--version" not in record["argv"] and "--help" not in record["argv"]  # type: ignore[operator]
    )
    main_env = cast("dict[str, str]", main_record["env"])
    assert main_env.get("_MERIDIAN_PI_CHILD_WAVE_TIMEOUT_MS") == expected_timeout
    assert main_env.get("_MERIDIAN_PI_TASK_PING_INTERVAL_MS") == expected_interval
