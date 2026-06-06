"""Unit tests for Claude agent-copy init scaffolding."""

from __future__ import annotations

import tomllib
from pathlib import Path

from meridian.lib.ops.init_ops import maybe_scaffold_claude_agent_copy

_BASE = '[settings]\ntargets = [".claude"]\n\n[dependencies.base]\npath = "../pkg"\n'


def _write(project_root: Path, body: str) -> Path:
    mars_toml = project_root / "mars.toml"
    mars_toml.write_text(body)
    return mars_toml


def _agent_copy(mars_toml: Path) -> object:
    with mars_toml.open("rb") as handle:
        payload = tomllib.load(handle)
    return payload["settings"]["meridian"]["agent_copy"]


def test_scaffolds_claude_agent_copy_when_claude_linked(tmp_path: Path) -> None:
    mars_toml = _write(tmp_path, _BASE)

    added = maybe_scaffold_claude_agent_copy(tmp_path, [".claude"])

    assert added is True
    agent_copy = _agent_copy(mars_toml)
    assert agent_copy == {"harnesses": ["claude"], "include_fanout": False}
    # Existing tables are preserved.
    with mars_toml.open("rb") as handle:
        payload = tomllib.load(handle)
    assert payload["settings"]["targets"] == [".claude"]
    assert payload["dependencies"]["base"]["path"] == "../pkg"


def test_scaffold_is_idempotent(tmp_path: Path) -> None:
    mars_toml = _write(tmp_path, _BASE)

    assert maybe_scaffold_claude_agent_copy(tmp_path, [".claude"]) is True
    first = mars_toml.read_text()
    assert maybe_scaffold_claude_agent_copy(tmp_path, [".claude"]) is False
    assert mars_toml.read_text() == first
    assert first.count("[settings.meridian.agent_copy]") == 1


def test_bare_claude_target_name_is_detected(tmp_path: Path) -> None:
    mars_toml = _write(tmp_path, _BASE)
    assert maybe_scaffold_claude_agent_copy(tmp_path, ["claude"]) is True
    assert _agent_copy(mars_toml) == {"harnesses": ["claude"], "include_fanout": False}


def test_non_claude_target_is_skipped(tmp_path: Path) -> None:
    mars_toml = _write(tmp_path, '[settings]\ntargets = [".codex"]\n')
    assert maybe_scaffold_claude_agent_copy(tmp_path, [".codex"]) is False
    assert "agent_copy" not in mars_toml.read_text()


def test_missing_mars_toml_is_noop(tmp_path: Path) -> None:
    assert maybe_scaffold_claude_agent_copy(tmp_path, [".claude"]) is False


def test_scaffolded_config_is_recognized_by_consumer(tmp_path: Path) -> None:
    """Writer/reader round-trip: scaffolded table enables the Claude gate."""
    from meridian.lib.launch.permissions import project_has_claude_agent_copy

    _write(tmp_path, _BASE)
    assert project_has_claude_agent_copy(tmp_path) is False
    assert maybe_scaffold_claude_agent_copy(tmp_path, [".claude"]) is True
    assert project_has_claude_agent_copy(tmp_path) is True
