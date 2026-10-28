"""System-instruction block ordering and skill type grouping."""

from __future__ import annotations

from meridian.lib.launch.composition import (
    ComposedLaunchContent,
    CompositionBlock,
    GuidancePhase,
    PromptDocument,
    render_system_instruction_blocks,
)


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
        guidance_blocks=(
            CompositionBlock("inventory", GuidancePhase.GUIDANCE, 0, "INVENTORY"),
            CompositionBlock("context-env", GuidancePhase.ENVIRONMENT, 0, "CONTEXT"),
        ),
        passthrough_system_fragments=("PASSTHROUGH",),
        user_task_prompt="USER",
        reference_items=(),
        prior_output="",
    )

    rendered = render_system_instruction_blocks(content)
    # Composition order: profile, skills sorted by type (principles first,
    # then guardrails, then reference/other), bootstrap, inventory, context,
    # report, passthrough. No principle bookend at the end. The completion
    # contract is user-turn content, not a system block.
    expected_sequence = (
        "PROFILE",
        "PRINCIPLE-A",
        "PRINCIPLE-B",
        "GUARDRAIL",
        "REF",
        "BOOTSTRAP",
        "INVENTORY",
        "CONTEXT",
        "REPORT",
        "PASSTHROUGH",
    )
    cursor = -1
    for part in expected_sequence:
        cursor = rendered.find(part, cursor + 1)
        assert cursor != -1, f"Expected '{part}' after position {cursor} in rendered output"
    # Principle bookend is gone — principles should NOT appear after REPORT
    report_pos = rendered.find("REPORT")
    passthrough_pos = rendered.find("PASSTHROUGH")
    between = rendered[report_pos:passthrough_pos]
    assert "PRINCIPLE-A" not in between
