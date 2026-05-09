import logging
from pathlib import Path

import pytest

from meridian.lib.catalog.agent import (
    FanoutEntry,
    ModelPolicyRule,
    load_agent_profile,
    parse_agent_profile,
    scan_agent_profiles,
)
from meridian.lib.diagnostics import capture_library_diagnostics


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
        ["name: Reviewer", "mode: primary", "fanout:", "  - alias: gpt55"],
    )

    profiles = scan_agent_profiles(project_root=project_root)

    assert [profile.name for profile in profiles] == ["Coder", "Reviewer"]
    assert [profile.path.parent for profile in profiles] == [
        agents_dir.resolve(),
        agents_dir.resolve(),
    ]
    assert profiles[1].mode == "primary"
    assert profiles[1].fanout == (FanoutEntry(entry_type="alias", value="gpt55"),)


def test_load_agent_profile_missing_error_points_to_mars_agents_path(tmp_path: Path) -> None:
    project_root = tmp_path / "repo"
    (project_root / ".mars" / "agents").mkdir(parents=True)
    _write_profile(project_root / ".mars" / "agents", "coder.md", ["name: Coder"])

    with pytest.raises(FileNotFoundError, match=r"Expected: \.mars/agents/reviewer\.md"):
        load_agent_profile("reviewer", project_root=project_root)


def test_parse_agent_profile_disallowed_tools(tmp_path: Path) -> None:
    profile_path = _write_profile(
        tmp_path,
        "coder.md",
        [
            "name: Coder",
            "tools:",
            "  - Read",
            "disallowed-tools:",
            "  - Bash",
            "  - WebSearch",
            "mcp-tools:",
            "  - mcpA",
        ],
    )

    profile = parse_agent_profile(profile_path)

    assert profile.tools == ("Read",)
    assert profile.disallowed_tools == ("Bash", "WebSearch")


def test_parse_agent_profile_models_preserves_supported_overrides(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    profile_path = _write_profile(
        tmp_path,
        "reviewer.md",
        [
            "name: Reviewer",
            "models:",
            "  gpt55:",
            "    effort: low",
            "    autocompact: 200000",
            "    lane: correctness",
            "  unknown-only:",
            "    custom_field: ok",
        ],
    )

    caplog.set_level(logging.WARNING, logger="meridian.lib.catalog.agent")

    profile = parse_agent_profile(profile_path)

    assert tuple(profile.models.keys()) == ("gpt55", "unknown-only")
    assert profile.models["gpt55"].effort == "low"
    assert profile.models["gpt55"].autocompact == 200000
    assert profile.models["unknown-only"].effort is None
    assert profile.models["unknown-only"].autocompact is None
    assert "uses legacy models" in caplog.text


def test_parse_agent_profile_fanout_is_display_only_alias_list(
    tmp_path: Path,
) -> None:
    profile_path = _write_profile(
        tmp_path,
        "reviewer.md",
        [
            "name: Reviewer",
            "models:",
            "  policy-only:",
            "    effort: low",
            "fanout:",
            "  - gpt54",
            "  - gpt55",
        ],
    )

    profile = parse_agent_profile(profile_path)

    assert tuple(profile.models.keys()) == ("policy-only",)
    assert profile.fanout == (
        FanoutEntry(entry_type="alias", value="gpt54"),
        FanoutEntry(entry_type="alias", value="gpt55"),
    )


def test_parse_agent_profile_model_policies_and_structured_fanout(tmp_path: Path) -> None:
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
            "fanout:",
            "  - alias: opus",
            "  - model: gemini-2.0-flash",
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
    assert profile.fanout == (
        FanoutEntry(entry_type="alias", value="opus"),
        FanoutEntry(entry_type="model", value="gemini-2.0-flash"),
    )


def test_parse_agent_profile_rejects_invalid_mode(tmp_path: Path) -> None:
    profile_path = _write_profile(tmp_path, "bad.md", ["name: Bad", "mode: worker"])

    with pytest.raises(ValueError, match="invalid mode"):
        parse_agent_profile(profile_path)


@pytest.mark.parametrize(
    "lines, match",
    [
        (
            [
                "model-policies:",
                "  - match:",
                "      model: gpt-5.5",
                "      alias: gpt",
                "    override:",
                "      effort: high",
            ],
            "exactly one match key",
        ),
        (
            [
                "model-policies:",
                "  - match:",
                "      model: gpt-5.5",
                "    override: {}",
            ],
            "at least one override",
        ),
    ],
)
def test_parse_agent_profile_rejects_invalid_model_policies(
    tmp_path: Path,
    lines: list[str],
    match: str,
) -> None:
    profile_path = _write_profile(tmp_path, "bad.md", ["name: Bad", *lines])

    with pytest.raises(ValueError, match=match):
        parse_agent_profile(profile_path)


def test_parse_agent_profile_rejects_unknown_model_policy_override_key(
    tmp_path: Path,
) -> None:
    profile_path = _write_profile(
        tmp_path,
        "bad.md",
        [
            "name: Bad",
            "model-policies:",
            "  - match: {model: gpt-5.5}",
            "    override:",
            "      temperature: 0.2",
        ],
    )

    with pytest.raises(ValueError, match="unknown override key 'temperature'"):
        parse_agent_profile(profile_path)


def test_parse_agent_profile_accepts_deferred_model_policy_list_override_keys(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    profile_path = _write_profile(
        tmp_path,
        "reviewer.md",
        [
            "name: Reviewer",
            "model-policies:",
            "  - match: {model: gpt-5.5}",
            "    override:",
            "      skills:",
            "        - review",
            "      tools:",
            "        - Read",
            "      disallowed-tools:",
            "        - Bash",
            "      mcp-tools:",
            "        - github",
        ],
    )
    caplog.set_level(logging.WARNING, logger="meridian.lib.catalog.agent")

    profile = parse_agent_profile(profile_path)

    assert profile.model_policies[0].overrides == {
        "skills": ["review"],
        "tools": ["Read"],
        "disallowed-tools": ["Bash"],
        "mcp-tools": ["github"],
    }
    assert "not-yet-supported list override keys" in caplog.text


@pytest.mark.parametrize(
    "fanout_lines",
    [
        ["fanout:", "  - alias: opus", "    model: claude-opus-4-6"],
        ["fanout:", "  - {}"],
    ],
)
def test_parse_agent_profile_rejects_invalid_structured_fanout(
    tmp_path: Path,
    fanout_lines: list[str],
) -> None:
    profile_path = _write_profile(tmp_path, "bad.md", ["name: Bad", *fanout_lines])

    with pytest.raises(ValueError, match="exactly one of alias or model"):
        parse_agent_profile(profile_path)


def test_parse_agent_profile_models_discards_invalid_entries_and_warns(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    profile_path = _write_profile(
        tmp_path,
        "planner.md",
        [
            "name: Planner",
            "models:",
            "  valid:",
            "    effort: medium",
            "  bad-effort:",
            "    effort: auto",
            "  bad-autocompact:",
            "    autocompact: 101",
            "  bad-autocompact-bool:",
            "    autocompact: true",
            "  '   ':",
            "    effort: low",
        ],
    )
    caplog.set_level(logging.WARNING, logger="meridian.lib.catalog.agent")

    profile = parse_agent_profile(profile_path)

    assert tuple(profile.models.keys()) == ("valid",)
    warning_text = "\n".join(record.message for record in caplog.records)
    assert "invalid models entry for 'bad-effort'" in warning_text
    assert "invalid models entry for 'bad-autocompact'" in warning_text
    assert "invalid models entry for 'bad-autocompact-bool'" in warning_text
    assert "empty models key" in warning_text


def test_scan_agent_profiles_warnings_can_be_captured_at_boundary(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    project_root = tmp_path / "repo"
    agents_dir = project_root / ".mars" / "agents"
    agents_dir.mkdir(parents=True)
    _write_profile(
        agents_dir,
        "planner.md",
        [
            "name: Planner",
            "effort: invalid",
            "autocompact: 150",
            "models:",
            "  bad-effort:",
            "    effort: auto",
        ],
    )
    caplog.set_level(logging.WARNING, logger="meridian.lib.catalog.agent")

    with capture_library_diagnostics() as diag:
        profiles = scan_agent_profiles(project_root=project_root)

    assert [profile.name for profile in profiles] == ["Planner"]
    assert caplog.records == []
    assert [record.getMessage() for record in diag.records] == [
        "Agent profile 'Planner' has unknown effort 'invalid'.",
        "Agent profile 'Planner' has autocompact 150 outside valid range.",
        "Agent profile 'Planner' has invalid models entry for 'bad-effort'; entry ignored.",
        "Agent profile 'Planner' uses legacy models without model-policies or fanout; "
        "models is deprecated for fan-out display and policy overrides.",
    ]


def test_parse_agent_profile_keeps_valid_profile_autocompact(tmp_path: Path) -> None:
    profile_path = _write_profile(
        tmp_path,
        "coder.md",
        [
            "name: Coder",
            "model: gpt-5.4",
            "autocompact: 200000",
        ],
    )

    profile = parse_agent_profile(profile_path)
    assert profile.autocompact == 200000


def test_parse_agent_profile_drops_out_of_range_profile_autocompact(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    profile_path = _write_profile(
        tmp_path,
        "coder.md",
        [
            "name: Coder",
            "model: gpt-5.4",
            "autocompact: 150",
        ],
    )
    caplog.set_level(logging.WARNING, logger="meridian.lib.catalog.agent")

    profile = parse_agent_profile(profile_path)
    assert profile.autocompact is None
    assert "outside valid range" in caplog.text
