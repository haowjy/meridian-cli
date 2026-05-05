from __future__ import annotations

from meridian.lib.launch.prompt import dedupe_skill_names as prompt_dedupe_skill_names
from meridian.lib.launch.resolve import dedupe_skill_names


def test_dedupe_skill_names_is_importable_from_resolve() -> None:
    assert callable(dedupe_skill_names)


def test_dedupe_skill_names_is_reexported_from_prompt() -> None:
    assert prompt_dedupe_skill_names is dedupe_skill_names


def test_dedupe_skill_names_preserves_first_seen_order() -> None:
    assert dedupe_skill_names([" alpha ", "beta", "alpha", "", " beta ", "gamma "]) == (
        "alpha",
        "beta",
        "gamma",
    )
