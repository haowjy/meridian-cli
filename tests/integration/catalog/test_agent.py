# qa-validated: orchestrator-opencode-fallback-runtime
from pathlib import Path

import pytest

from meridian.lib.catalog.agent import (
    AgentProfile,
    load_agent_profile,
    parse_agent_profile,
    scan_agent_profiles,
)


def _write_profile(tmp_path: Path, filename: str, frontmatter_lines: list[str]) -> Path:
    profile_path = tmp_path / filename
    profile_path.write_text(
        "\n".join(["---", *frontmatter_lines, "---", "", "Profile body.", ""]) + "\n",
        encoding="utf-8",
    )
    return profile_path


def test_agent_profile_fields_are_identity_and_content_only() -> None:
    assert tuple(AgentProfile.model_fields) == (
        "name",
        "description",
        "mode",
        "skills",
        "model_invocable",
        "body",
        "path",
        "raw_content",
    )


def test_scan_agent_profiles_reads_real_mars_agents_directory(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    agents_dir = project_root / ".mars" / "agents"
    agents_dir.mkdir(parents=True)
    _write_profile(agents_dir, "coder.md", ["name: Coder"])
    _write_profile(
        agents_dir,
        "reviewer.md",
        [
            "name: Reviewer",
            "mode: primary",
            "skills: [review, review, diff]",
        ],
    )

    profiles = scan_agent_profiles(project_root=project_root)

    assert [profile.name for profile in profiles] == ["Coder", "Reviewer"]
    assert profiles[1].mode == "primary"
    assert profiles[1].skills == ("review", "review", "diff")


def test_load_agent_profile_missing_error_points_to_mars_agents_path(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    (project_root / ".mars" / "agents").mkdir(parents=True)
    _write_profile(project_root / ".mars" / "agents", "coder.md", ["name: Coder"])

    with pytest.raises(FileNotFoundError, match=r"Expected: \.mars/agents/reviewer\.md"):
        load_agent_profile("reviewer", project_root=project_root)


def test_parse_agent_profile_ignores_legacy_fanout(tmp_path: Path) -> None:
    profile_path = _write_profile(
        tmp_path,
        "bad.md",
        ["name: Bad", "fanout:", "  - gpt54", "  - gpt55"],
    )

    profile = parse_agent_profile(profile_path)

    assert profile.name == "Bad"


def test_parse_agent_profile_ignores_legacy_models(tmp_path: Path) -> None:
    profile_path = _write_profile(
        tmp_path,
        "bad.md",
        ["name: Bad", "models:", "  gpt55:", "    effort: low"],
    )

    profile = parse_agent_profile(profile_path)

    assert profile.name == "Bad"


def test_parse_agent_profile_ignores_routing_and_inventory_display_fields(
    tmp_path: Path,
) -> None:
    profile_path = _write_profile(
        tmp_path,
        "bad.md",
        [
            "name: Bad",
            "model: gpt55",
            "harness: codex",
            "sandbox: workspace-write",
            "effort: high",
            "approval: auto",
            "autocompact: 1000",
            "autocompact_pct: 50",
            "tools: allow",
            "mcp-tools: [foo]",
            "model-policies:",
            "  - match: {alias: gpt55}",
            "    override: {effort: low}",
            "  - match: {model: gpt-5.4-mini}",
            "    override: {model: gpt-5.4-mini}",
        ],
    )

    profile = parse_agent_profile(profile_path)

    assert profile.name == "Bad"
    assert profile.mode == "subagent"
    assert "model" not in AgentProfile.model_fields
    assert "fanout" not in AgentProfile.model_fields


def test_parse_agent_profile_mode_defaults_to_subagent_for_invalid_value(tmp_path: Path) -> None:
    profile_path = _write_profile(
        tmp_path,
        "bad.md",
        ["name: Bad", "mode: daemon"],
    )

    profile = parse_agent_profile(profile_path)

    assert profile.mode == "subagent"


def test_parse_agent_profile_defaults_model_invocable_to_true(tmp_path: Path) -> None:
    profile_path = _write_profile(
        tmp_path,
        "agent.md",
        ["name: Agent", "model-invocable: nope"],
    )

    profile = parse_agent_profile(profile_path)

    assert profile.model_invocable is True


def test_scan_agent_profiles_skips_unreadable_profile_without_blocking_catalog(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "repo"
    agents_dir = project_root / ".mars" / "agents"
    agents_dir.mkdir(parents=True)
    _write_profile(agents_dir, "coder.md", ["name: Coder"])
    bad_path = agents_dir / "broken.md"
    bad_path.mkdir()

    profiles = scan_agent_profiles(project_root=project_root)

    assert [profile.name for profile in profiles] == ["Coder"]
