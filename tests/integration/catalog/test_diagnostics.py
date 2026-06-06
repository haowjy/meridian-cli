"""Integration test for diagnostics + agent inventory prompt.

Verifies that package-authoring validation warnings are not emitted from
runtime catalog scanning. Requires real filesystem I/O (writes an agent
profile to disk).

# qa-validated: test-suite-redesign
"""

from pathlib import Path

from meridian.lib.diagnostics import capture_library_diagnostics
from meridian.lib.launch.prompt_context import build_agent_inventory_prompt


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


def test_build_launch_context_does_not_leak_library_warnings_to_stderr(
    tmp_path: Path,
) -> None:
    """Structural guard: profile metadata does not emit runtime warnings."""

    _write_profile(tmp_path)
    with capture_library_diagnostics() as diag:
        build_agent_inventory_prompt(project_root=tmp_path)

    assert diag.records == []
