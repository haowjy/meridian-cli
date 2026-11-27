from pathlib import Path

from meridian.lib.harness.codex_rules import (
    MERIDIAN_RULES_FILE_CONTENT,
    RULES_FILES,
    ensure_codex_meridian_rules,
)

# What 0.27.10 writes. A release's file never changes: a build rewrites a file whose bytes
# differ from its own, so an older build would undo a changed one on every launch.
V0_27_10_MERIDIAN_RULES = (
    "# This file is owned and rewritten by Meridian.\n"
    'prefix_rule(pattern=["meridian", "spawn"], decision="allow")\n'
)


def test_meridian_rules_is_byte_identical_to_0_27_10() -> None:
    assert MERIDIAN_RULES_FILE_CONTENT == V0_27_10_MERIDIAN_RULES


def test_message_grant_goes_in_its_own_file_and_leaves_the_first_alone(tmp_path: Path) -> None:
    rules = tmp_path / "rules"
    rules.mkdir()
    first = rules / "meridian.rules"
    first.write_text(V0_27_10_MERIDIAN_RULES, encoding="utf-8")
    inode = first.stat().st_ino

    resolution = ensure_codex_meridian_rules(
        {"CODEX_HOME": str(tmp_path)}, child_cwd=tmp_path, files=RULES_FILES
    )

    assert resolution.rules_file_error is None
    assert first.stat().st_ino == inode
    assert first.read_text(encoding="utf-8") == V0_27_10_MERIDIAN_RULES
    assert (rules / "meridian-message.rules").read_text(encoding="utf-8") == (
        "# This file is owned and rewritten by Meridian.\n"
        'prefix_rule(pattern=["meridian", "message"], decision="allow")\n'
    )
