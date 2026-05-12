"""Prompt assembly tests that guard against context and prompt injection."""

from pathlib import Path

from meridian.lib.core.domain import SkillContent
from meridian.lib.launch.composition import (
    ComposedLaunchContent,
    PromptDocument,
    render_system_instruction_blocks,
)
from meridian.lib.launch.prompt import compose_run_prompt_text
from meridian.lib.launch.reference import load_reference_items


def test_compose_prompt_keeps_context_isolated_and_sanitized(tmp_path: Path) -> None:
    safe_ref = tmp_path / "safe.md"
    hidden_ref = tmp_path / "hidden.md"
    safe_ref.write_text("Safe context {{CTX}}", encoding="utf-8")
    hidden_ref.write_text("INJECTION: should never leak", encoding="utf-8")

    # Load only the safe reference - hidden_ref should not be included
    loaded_refs = load_reference_items([safe_ref])
    skill = SkillContent(
        name="worker",
        description="",
        content="Skill content",
        path=str(tmp_path / "worker.md"),
    )
    user_prompt = (
        "**IMPORTANT - As your FINAL action**, write a report of your work to: "
        "`/tmp/stale.md`\n\nImplement the change with {{CTX}}."
    )

    composed = compose_run_prompt_text(
        skills=[skill],
        references=loaded_refs,
        user_prompt=user_prompt,
        template_variables={"CTX": "context"},
    )

    # Hidden content should never appear (only safe_ref was loaded)
    assert "INJECTION: should never leak" not in composed
    # Report instruction appears exactly once
    assert composed.count("Your final assistant message must be the run report.") == 1
    # Stale report path is stripped
    assert "/tmp/stale.md" not in composed
    # Safe reference path appears in header
    assert safe_ref.as_posix() in composed
    # File content is now inlined (new behavior)
    assert "# Reference:" in composed
    # Template variable CTX is substituted in user prompt
    assert "Implement the change with context." in composed
    # Safe file content is included (inlined)
    assert "Safe context" in composed


def test_compose_prompt_with_directory_renders_tree(tmp_path: Path) -> None:
    """Test that directories are rendered as trees, not inlined."""
    # Create directory structure
    subdir = tmp_path / "mydir"
    subdir.mkdir()
    (subdir / "file1.py").write_text("# Python file")
    (subdir / "file2.md").write_text("# Markdown")
    nested = subdir / "nested"
    nested.mkdir()
    (nested / "deep.txt").write_text("Deep file")

    loaded_refs = load_reference_items([subdir])

    composed = compose_run_prompt_text(
        skills=[],
        references=loaded_refs,
        user_prompt="Explore the directory",
    )

    # Directory should be rendered as tree
    assert "# Reference:" in composed
    assert "mydir/" in composed
    # Tree structure indicators
    assert "├──" in composed or "└──" in composed
    # Files should appear in tree
    assert "file1.py" in composed
    assert "file2.md" in composed
    # Nested directory shown
    assert "nested/" in composed
    # File contents should NOT be inlined for directories
    assert "# Python file" not in composed
    assert "# Markdown" not in composed
    assert "Deep file" not in composed


def test_compose_prompt_mixed_files_and_directories(tmp_path: Path) -> None:
    """Test mixing files and directories in references."""
    # Create a file
    single_file = tmp_path / "single.md"
    single_file.write_text("File content here")

    # Create a directory
    mydir = tmp_path / "mydir"
    mydir.mkdir()
    (mydir / "inner.py").write_text("# Inner")

    loaded_refs = load_reference_items([single_file, mydir])

    composed = compose_run_prompt_text(
        skills=[],
        references=loaded_refs,
        user_prompt="Check both",
    )

    # Single file should be inlined
    assert "File content here" in composed
    # Directory should be tree
    assert "mydir/" in composed
    assert "inner.py" in composed
    # Directory content should NOT be inlined
    assert "# Inner" not in composed


def test_system_instruction_renders_reordered_blocks_and_skill_type_groups() -> None:
    content = ComposedLaunchContent(
        supplemental_documents=(
            PromptDocument(kind="skill", logical_name="ref", path="/ref", content="REF"),
            PromptDocument(
                kind="skill",
                logical_name="principle-a",
                path="/principle-a",
                content="PRINCIPLE-A",
                skill_type="principle",
            ),
            PromptDocument(kind="bootstrap", logical_name="b", path="/b", content="BOOTSTRAP"),
            PromptDocument(
                kind="skill",
                logical_name="guardrail",
                path="/guardrail",
                content="GUARDRAIL",
                skill_type="guardrail",
            ),
            PromptDocument(
                kind="skill",
                logical_name="principle-b",
                path="/principle-b",
                content="PRINCIPLE-B",
                skill_type="principle",
            ),
        ),
        agent_profile_body="PROFILE",
        report_instruction="REPORT",
        inventory_prompt="INVENTORY",
        context_prompt="CONTEXT",
        completion_contract="GOAL",
        passthrough_system_fragments=("PASSTHROUGH",),
        user_task_prompt="USER",
        reference_items=(),
        prior_output="",
    )

    rendered = render_system_instruction_blocks(content)
    expected_sequence = (
        "PROFILE",
        "GOAL",
        "PRINCIPLE-A",
        "PRINCIPLE-B",
        "GUARDRAIL",
        "REF",
        "BOOTSTRAP",
        "INVENTORY",
        "CONTEXT",
        "REPORT",
        "PRINCIPLE-A",
        "PRINCIPLE-B",
        "PASSTHROUGH",
    )
    cursor = -1
    for part in expected_sequence:
        cursor = rendered.find(part, cursor + 1)
        assert cursor != -1
