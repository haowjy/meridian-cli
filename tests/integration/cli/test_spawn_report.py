"""CLI coverage for report selection during spawn finalization."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from meridian.lib.streaming.control_socket import socket_directory
from tests.conftest import posix_only

_CLI_TIMEOUT_SECONDS = 20


def _run_fake_claude_spawn(tmp_path: Path, *, explicit_report: str | None) -> dict[str, object]:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "meridian.toml").write_text(
        "[project]\n"
        f'id = "report-test-{tmp_path.name}"\n'
        "\n[spawn]\n"
        "deny_headless_harnesses = []\n",
        encoding="utf-8",
    )
    runtime_root = tmp_path / "runtime"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    report_command = ""
    if explicit_report is not None:
        report_path = runtime_root / "spawns" / "p1" / "launches" / "1" / "report.md"
        report_command = (
            'if [ -n "$MERIDIAN_SPAWN_ID" ]; then\n'
            f"mkdir -p {shlex.quote(str(report_path.parent))}\n"
            f"printf '%s\\n' {shlex.quote(explicit_report)} > "
            f"{shlex.quote(str(report_path))}\n"
            "fi\n"
        )
    fake_claude = fake_bin / "claude"
    fake_claude.write_text(
        "#!/bin/sh\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = "--version" ]; then version=1; fi\n'
        '  if [ "$1" = "--session-id" ]; then shift; session_id=$1; fi\n'
        "  shift\n"
        "done\n"
        'if [ "$version" = "1" ]; then echo "2.1.0 (Claude Code)"; exit 0; fi\n'
        "read -r prompt || true\n"
        + report_command
        + "printf '{\"type\":\"system\",\"subtype\":\"init\","
        "\"session_id\":\"%s\"}\\n' \"$session_id\"\n"
        "printf '{\"type\":\"result\",\"subtype\":\"success\","
        "\"result\":\"fallback assistant\",\"session_id\":\"%s\"}\\n' "
        '"$session_id"\n',
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)

    env = {
        "HOME": str(tmp_path / "home"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "USER": os.environ.get("USER", "test"),
        "MERIDIAN_HOME": str(tmp_path / "meridian-home"),
        "_MERIDIAN_RUNTIME_DIR": str(runtime_root),
        "FAKE_SOCKET_ROOT": str(socket_directory().parent),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os, runpy; from pathlib import Path; "
            "from meridian.lib.streaming import control_socket; "
            "control_socket._POSIX_SOCKET_ROOT = Path(os.environ['FAKE_SOCKET_ROOT']); "
            "runpy.run_module('meridian', run_name='__main__')",
            "--json",
            "--harness",
            "claude",
            "spawn",
            "-a",
            "",
            "-m",
            "claude-haiku-4-5",
            "-p",
            "report selection",
        ],
        cwd=project_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=_CLI_TIMEOUT_SECONDS,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.integration
@posix_only
def test_report_prefers_explicit_report_md(tmp_path: Path) -> None:
    output = _run_fake_claude_spawn(tmp_path, explicit_report="# Explicit\n\nDone.")

    assert output["report"] == "# Explicit\n\nDone."


@pytest.mark.integration
@posix_only
def test_report_falls_back_to_last_message(tmp_path: Path) -> None:
    output = _run_fake_claude_spawn(tmp_path, explicit_report=None)

    assert output["report"] == "# Report\n\nfallback assistant"
