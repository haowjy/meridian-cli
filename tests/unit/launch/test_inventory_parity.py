"""Parity guard for Python fallback inventory vs mars launch-bundle format.

mars-agents is not invoked here: CI pins a released mars wheel that may lag the
harness-aware inventory contract, and bundle generation needs a full mars project
fixture. These golden files snapshot the mars v4 harness-aware inventory_prompt
shape that both mars and the Python fallback must keep aligned.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from meridian.lib.catalog.agent import scan_agent_profiles
from meridian.lib.launch.prompt_context import build_agent_inventory_prompt

_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "inventory_parity"


def _parity_project(tmp_path: Path) -> Path:
    project_root = tmp_path / "project"
    mars_dir = project_root / ".mars"
    shutil.copytree(_FIXTURE_ROOT / "agents", mars_dir / "agents")
    shutil.copy(_FIXTURE_ROOT / "native-agents.json", mars_dir / "native-agents.json")
    return project_root


@pytest.mark.parametrize(
    ("harness_id", "expected_name"),
    [
        ("claude", "expected_claude_inventory.txt"),
        ("codex", "expected_codex_inventory.txt"),
    ],
)
def test_fallback_inventory_matches_mars_format_snapshot(
    tmp_path: Path,
    harness_id: str,
    expected_name: str,
) -> None:
    project_root = _parity_project(tmp_path)
    agents = scan_agent_profiles(project_root=project_root)
    rendered = build_agent_inventory_prompt(
        project_root=project_root,
        agents=agents,
        harness_id=harness_id,
    )
    expected = (_FIXTURE_ROOT / expected_name).read_text(encoding="utf-8").strip()

    assert rendered == expected
