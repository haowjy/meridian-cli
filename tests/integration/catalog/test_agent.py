# qa-validated: orchestrator-opencode-fallback-runtime
from pathlib import Path

import pytest

from meridian.lib.catalog.agent import (
    ModelPolicyRule,
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
            "model-policies:",
            "  - match: {alias: gpt55}",
            "    override: {effort: medium}",
        ],
    )

    profiles = scan_agent_profiles(project_root=project_root)

    assert [profile.name for profile in profiles] == ["Coder", "Reviewer"]
    assert profiles[1].mode == "primary"
    assert profiles[1].model_policies[0].no_fallback is False


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


def test_parse_agent_profile_ignores_legacy_fallback_order(tmp_path: Path) -> None:
    profile_path = _write_profile(
        tmp_path,
        "bad.md",
        [
            "name: Bad",
            "model-policies:",
            "  - match: {alias: gpt55}",
            "    fallback-order: 1",
            "    override: {effort: high}",
        ],
    )

    profile = parse_agent_profile(profile_path)

    assert profile.model_policies == (
        ModelPolicyRule(
            match_type="alias",
            match_value="gpt55",
            overrides={"effort": "high"},
        ),
    )


def test_parse_agent_profile_model_policies_parse(tmp_path: Path) -> None:
    profile_path = _write_profile(
        tmp_path,
        "reviewer.md",
        [
            "name: Reviewer",
            "mode: primary",
            "model-policies:",
            "  - match:",
            "      model: gpt-5.5",
            "    override:",
            "      effort: high",
            "      autocompact: 80",
            "  - match:",
            "      alias: opus",
            "    override:",
            "      harness: claude",
        ],
    )

    profile = parse_agent_profile(profile_path)

    assert profile.mode == "primary"
    assert profile.model_policies == (
        ModelPolicyRule(
            match_type="model",
            match_value="gpt-5.5",
            overrides={"effort": "high", "autocompact": 80},
        ),
        ModelPolicyRule(
            match_type="alias",
            match_value="opus",
            overrides={"harness": "claude"},
        ),
    )


def test_parse_agent_profile_model_policies_implicit_fallback_order(tmp_path: Path) -> None:
    profile_path = _write_profile(
        tmp_path,
        "reviewer.md",
        [
            "name: Reviewer",
            "model-policies:",
            "  - match: {alias: gpt55}",
            "    override: {effort: medium}",
            "  - match: {model: gpt-5.4}",
            "    override: {effort: high}",
            "  - match: {model-glob: 'gpt-*'}",
            "    override: {approval: auto}",
            "  - match: {alias: codex}",
            "    no-fallback: true",
            "    override: {effort: low}",
        ],
    )

    profile = parse_agent_profile(profile_path)

    fallback_candidates = [
        rule.match_value
        for rule in profile.model_policies
        if rule.match_type in {"alias", "model"} and not rule.no_fallback
    ]
    assert fallback_candidates == ["gpt55", "gpt-5.4"]


def test_parse_agent_profile_model_glob_rules_are_always_no_fallback(tmp_path: Path) -> None:
    profile_path = _write_profile(
        tmp_path,
        "reviewer.md",
        [
            "name: Reviewer",
            "model-policies:",
            "  - match: {model-glob: 'gpt-*'}",
            "    override: {effort: medium}",
        ],
    )

    profile = parse_agent_profile(profile_path)

    assert profile.model_policies[0].no_fallback is True


def test_parse_agent_profile_model_policies_accepts_empty_override_with_fallback_candidate(
    tmp_path: Path,
) -> None:
    profile_path = _write_profile(
        tmp_path,
        "reviewer.md",
        [
            "name: Reviewer",
            "model-policies:",
            "  - match: {alias: claude-opus-4-6}",
            "    override: {}",
        ],
    )

    profile = parse_agent_profile(profile_path)

    assert profile.model_policies == (
        ModelPolicyRule(
            match_type="alias",
            match_value="claude-opus-4-6",
            no_fallback=False,
            overrides={},
        ),
    )


def test_parse_agent_profile_model_policies_accepts_missing_override_with_fallback_candidate(
    tmp_path: Path,
) -> None:
    profile_path = _write_profile(
        tmp_path,
        "reviewer.md",
        [
            "name: Reviewer",
            "model-policies:",
            "  - match: {alias: claude-opus-4-6}",
        ],
    )

    profile = parse_agent_profile(profile_path)

    assert profile.model_policies == (
        ModelPolicyRule(
            match_type="alias",
            match_value="claude-opus-4-6",
            no_fallback=False,
            overrides={},
        ),
    )


def test_parse_agent_profile_ignores_noop_non_fallback_policy_rule(tmp_path: Path) -> None:
    profile_path = _write_profile(
        tmp_path,
        "bad.md",
        [
            "name: Bad",
            "model-policies:",
            "  - match:",
            "      model: gpt-5.5",
            "    no-fallback: true",
            "    override: {}",
        ],
    )

    profile = parse_agent_profile(profile_path)

    assert profile.model_policies == ()


@pytest.mark.parametrize(
    "lines",
    [
        [
            "model-policies:",
            "  - match:",
            "      model: gpt-5.5",
            "      alias: gpt",
            "    override:",
            "      effort: high",
        ],
    ],
)
def test_parse_agent_profile_ignores_invalid_model_policy_rules(
    tmp_path: Path,
    lines: list[str],
) -> None:
    profile_path = _write_profile(tmp_path, "bad.md", ["name: Bad", *lines])

    profile = parse_agent_profile(profile_path)

    assert profile.model_policies == ()


def test_parse_agent_profile_filters_unknown_model_policy_override_key(tmp_path: Path) -> None:
    profile_path = _write_profile(
        tmp_path,
        "mixed.md",
        [
            "name: Mixed",
            "model-policies:",
            "  - match: {model: gpt-5.5}",
            "    override:",
            "      effort: high",
            "      temperature: 0.2",
        ],
    )

    profile = parse_agent_profile(profile_path)

    assert profile.model_policies == (
        ModelPolicyRule(
            match_type="model",
            match_value="gpt-5.5",
            no_fallback=False,
            overrides={"effort": "high"},
        ),
    )


def test_parse_agent_profile_ignores_invalid_optional_fields(tmp_path: Path) -> None:
    profile_path = _write_profile(
        tmp_path,
        "bad.md",
        [
            "name: Bad",
            "mode: daemon",
            "approval: nope",
            "autocompact: 101",
            "autocompact_pct: 0",
            "tools: maybe",
        ],
    )

    profile = parse_agent_profile(profile_path)

    assert profile.mode == "subagent"
    assert profile.approval is None
    assert profile.autocompact is None
    assert profile.autocompact_pct is None
    assert profile.tools is None


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
