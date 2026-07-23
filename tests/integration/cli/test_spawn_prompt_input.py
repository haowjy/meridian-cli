"""CLI integration coverage for explicit spawn prompt sources."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import posix_only
from tests.support.executables import prepend_fake_executables


@pytest.mark.integration
@posix_only
def test_spawn_with_reference_does_not_read_silent_open_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    (project_root / ".meridian").mkdir(parents=True)
    (project_root / ".meridian" / "id").write_text("prompt-input-test", encoding="utf-8")
    (project_root / "meridian.toml").write_text(
        "[spawn]\ndeny_headless_harnesses = []\n",
        encoding="utf-8",
    )
    reference = project_root / "reference.md"
    reference.write_text("Reference context.\n", encoding="utf-8")

    # CI runners have no real harness binaries; Mars validates harness
    # installation even for --dry-run, so a stub codex must be on PATH.
    prepend_fake_executables(monkeypatch, tmp_path, "codex")
    env = os.environ.copy()
    env["MERIDIAN_HOME"] = (tmp_path / "home").as_posix()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "meridian",
            "--harness",
            "codex",
            "spawn",
            "-a",
            "",
            "--bg",
            "--dry-run",
            "-f",
            reference.as_posix(),
        ],
        cwd=project_root,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        return_code = process.wait(timeout=3)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stdin is not None:
            process.stdin.close()

    assert return_code == 0


@pytest.mark.integration
@posix_only
def test_spawn_prompt_file_stdin_reaches_dry_run_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    (project_root / ".meridian").mkdir(parents=True)
    (project_root / ".meridian" / "id").write_text("prompt-stdin-test", encoding="utf-8")
    (project_root / "meridian.toml").write_text(
        "[spawn]\ndeny_headless_harnesses = []\n",
        encoding="utf-8",
    )

    prepend_fake_executables(monkeypatch, tmp_path, "codex")
    env = os.environ.copy()
    env["MERIDIAN_HOME"] = (tmp_path / "home").as_posix()
    prompt = b"Prompt supplied on stdin.\nSecond line.\n"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "meridian",
            "--format",
            "json",
            "--harness",
            "codex",
            "spawn",
            "-a",
            "",
            "--bg",
            "--dry-run",
            "--prompt-file",
            "-",
        ],
        cwd=project_root,
        env=env,
        input=prompt,
        capture_output=True,
        timeout=3,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode()
    output = json.loads(result.stdout)
    assert output["status"] == "dry-run"
    assert output["composed_prompt"] == prompt.decode().strip()


@pytest.mark.integration
@posix_only
def test_spawn_rejects_subcommand_typo_but_accepts_unrelated_bare_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    (project_root / ".meridian").mkdir(parents=True)
    (project_root / ".meridian" / "id").write_text("prompt-typo-test", encoding="utf-8")
    (project_root / "meridian.toml").write_text(
        "[spawn]\ndeny_headless_harnesses = []\n",
        encoding="utf-8",
    )
    prepend_fake_executables(monkeypatch, tmp_path, "codex")
    env = os.environ.copy()
    env["MERIDIAN_HOME"] = (tmp_path / "home").as_posix()
    base_command = [
        sys.executable,
        "-m",
        "meridian",
        "--harness",
        "codex",
        "spawn",
    ]

    typo = subprocess.run(
        [*base_command, "statsu"],
        cwd=project_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=3,
        check=False,
    )
    prompt = subprocess.run(
        [*base_command, "investigate", "-a", "", "--bg", "--dry-run", "--format", "json"],
        cwd=project_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=3,
        check=False,
    )
    explicit_prompt = subprocess.run(
        [*base_command, "-p", "statsu", "-a", "", "--bg", "--dry-run", "--format", "json"],
        cwd=project_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=3,
        check=False,
    )

    assert typo.returncode != 0
    assert "did you mean 'meridian spawn stats'?" in typo.stderr.lower()
    assert prompt.returncode == 0, prompt.stderr
    assert json.loads(prompt.stdout)["composed_prompt"] == "investigate"
    assert explicit_prompt.returncode == 0, explicit_prompt.stderr
    assert json.loads(explicit_prompt.stdout)["composed_prompt"] == "statsu"
