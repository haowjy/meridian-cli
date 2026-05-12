# qa-validated: test-suite-redesign
"""Tests for OpenCode spec projection and system prompt materialization."""

from __future__ import annotations

import json
from pathlib import Path

from meridian.lib.harness.connections.opencode_http import _materialize_system_prompt
from meridian.lib.harness.projections.project_opencode_streaming import (
    project_opencode_spec_to_serve_command,
)
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver


def test_opencode_streaming_serve_command_keeps_verbatim_extra_args_tail() -> None:
    command = project_opencode_spec_to_serve_command(
        ResolvedLaunchSpec(
            prompt="hello",
            extra_args=("--port", "9999"),
            permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
        ),
        host="127.0.0.1",
        port=4096,
    )

    assert command[-2:] == ["--port", "9999"]


def test_materialize_system_prompt_writes_temp_file_and_injects_instruction(tmp_path: Path) -> None:
    spawn_dir = tmp_path / "spawns" / "p-test"
    spawn_dir.mkdir(parents=True)
    env: dict[str, str] = {}

    _materialize_system_prompt(spawn_dir, "You are an agent.", env)

    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    instructions = config["instructions"]
    assert len(instructions) == 1
    # File is a temp file, not in spawn_dir
    prompt_path = Path(instructions[0])
    assert prompt_path.exists()
    assert prompt_path.read_text(encoding="utf-8") == "You are an agent."
    assert "meridian-sysprompt-" in prompt_path.name
    prompt_path.unlink()


def test_materialize_system_prompt_merges_with_existing_config_content(tmp_path: Path) -> None:
    spawn_dir = tmp_path / "spawns" / "p-merge"
    spawn_dir.mkdir(parents=True)
    existing = json.dumps({"permission": {"external_directory": {"/some/path": "allow"}}})
    env: dict[str, str] = {"OPENCODE_CONFIG_CONTENT": existing}

    _materialize_system_prompt(spawn_dir, "system text", env)

    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    # Existing permission config preserved
    assert config["permission"] == {"external_directory": {"/some/path": "allow"}}
    # Instructions added as temp file
    assert len(config["instructions"]) == 1
    prompt_path = Path(config["instructions"][0])
    assert prompt_path.exists()
    prompt_path.unlink()


def test_materialize_system_prompt_merges_with_existing_instructions(tmp_path: Path) -> None:
    spawn_dir = tmp_path / "spawns" / "p-existing-instructions"
    spawn_dir.mkdir(parents=True)
    existing = json.dumps({"instructions": ["/existing/agents.md"]})
    env: dict[str, str] = {"OPENCODE_CONFIG_CONTENT": existing}

    _materialize_system_prompt(spawn_dir, "more instructions", env)

    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert config["instructions"][0] == "/existing/agents.md"
    assert len(config["instructions"]) == 2
    prompt_path = Path(config["instructions"][1])
    assert prompt_path.exists()
    prompt_path.unlink()


def test_materialize_system_prompt_noop_when_system_is_none(tmp_path: Path) -> None:
    spawn_dir = tmp_path / "spawns" / "p-none"
    spawn_dir.mkdir(parents=True)
    env: dict[str, str] = {}

    _materialize_system_prompt(spawn_dir, None, env)

    assert "OPENCODE_CONFIG_CONTENT" not in env


def test_materialize_system_prompt_noop_when_system_is_whitespace(tmp_path: Path) -> None:
    spawn_dir = tmp_path / "spawns" / "p-whitespace"
    spawn_dir.mkdir(parents=True)
    env: dict[str, str] = {}

    _materialize_system_prompt(spawn_dir, "   \n  ", env)

    assert "OPENCODE_CONFIG_CONTENT" not in env
