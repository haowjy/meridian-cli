"""Agent profile spawn-capability field parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.catalog.agent import (
    MeridianCapabilities,
    _parse_meridian_capabilities,
    parse_agent_profile,
)


def _write_profile(tmp_path: Path, filename: str, frontmatter_lines: list[str]) -> Path:
    profile_path = tmp_path / filename
    profile_path.write_text(
        "\n".join(["---", *frontmatter_lines, "---", "", "Profile body.", ""]) + "\n",
        encoding="utf-8",
    )
    return profile_path


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ({"spawn": True}, MeridianCapabilities(spawn=True)),
        ({"spawn": False, "other": True}, MeridianCapabilities(spawn=False)),
        ({" spawn ": True}, MeridianCapabilities(spawn=True)),
        ({"": True, "spawn": False}, MeridianCapabilities(spawn=False)),
        ({"spawn": 1}, None),
        ({"spawn": "false"}, None),
        ({"spawn": True, "bad": 0}, MeridianCapabilities(spawn=True)),
        ("not-a-dict", None),
        ([], None),
    ],
)
def test_parse_meridian_capabilities(
    value: object,
    expected: MeridianCapabilities | None,
) -> None:
    assert _parse_meridian_capabilities(value) == expected


def test_parse_agent_profile_reads_subagents_and_meridian_capabilities(tmp_path: Path) -> None:
    profile_path = _write_profile(
        tmp_path,
        "tech-lead.md",
        [
            "name: Tech Lead",
            "subagents: [explorer, coder, reviewer]",
            "meridian-capabilities:",
            "  spawn: false",
            "  notify: true",
        ],
    )

    profile = parse_agent_profile(profile_path)

    assert profile.subagents == ("explorer", "coder", "reviewer")
    assert profile.meridian_capabilities == MeridianCapabilities(spawn=False)


def test_parse_agent_profile_defaults_spawn_capability_fields(tmp_path: Path) -> None:
    profile_path = _write_profile(tmp_path, "explorer.md", ["name: Explorer"])

    profile = parse_agent_profile(profile_path)

    assert profile.subagents == ()
    assert profile.meridian_capabilities is None
