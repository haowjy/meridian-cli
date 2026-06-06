"""Integration test for diagnostics + agent catalog scanning.

Verifies that package-authoring validation warnings are not emitted from
runtime catalog scanning. Requires real filesystem I/O (writes an agent
profile to disk).

# qa-validated: test-suite-redesign
"""

from pathlib import Path

from meridian.lib.catalog.agent import scan_agent_profiles
from meridian.lib.diagnostics import capture_library_diagnostics


def _write_profile(project_root: Path) -> None:
    agents_dir = project_root / ".mars" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "legacy.md").write_text(
        "\n".join(
            [
                "---",
                "name: Legacy",
                "model-policies:",
                "  - match: {alias: gpt55}",
                "    override: {effort: medium}",
                "---",
                "",
                "Profile body.",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_scan_agent_profiles_does_not_leak_library_warnings_to_stderr(
    tmp_path: Path,
) -> None:
    """Structural guard: profile metadata does not emit runtime warnings."""

    _write_profile(tmp_path)
    with capture_library_diagnostics() as diag:
        list(scan_agent_profiles(project_root=tmp_path))

    assert diag.records == []
