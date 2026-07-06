from meridian.lib.core.spawn_start import (
    compact_prompt_summary,
    derive_display_label,
    resolve_spawn_display_label,
)


def test_compact_prompt_summary_collapses_whitespace_and_truncates() -> None:
    assert compact_prompt_summary("  hello\n\nworld  ") == "hello world"
    long_prompt = "x" * 60
    assert compact_prompt_summary(long_prompt) == f"{'x' * 47}..."


def test_derive_display_label_only_when_goal_and_desc_absent() -> None:
    assert derive_display_label(goal=None, desc=None, prompt="do the thing") == "do the thing"
    assert derive_display_label(goal="ship it", desc=None, prompt="ignored") is None
    assert derive_display_label(goal=None, desc="summary", prompt="ignored") is None


def test_resolve_spawn_display_label_prefers_goal_then_desc_then_display_label() -> None:
    assert resolve_spawn_display_label("goal", "desc", "label") == "goal"
    assert resolve_spawn_display_label(None, "desc", "label") == "desc"
    assert resolve_spawn_display_label(None, None, "label") == "label"
    assert resolve_spawn_display_label(None, None, None) is None
