"""The one line of a message's text `meridian inbox list` shows (`fields.preview`)."""

import pytest

from meridian.lib.inbox.fields import preview


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        # A line break or tab is a space before controls go, or two words run together.
        ("deploy\nnow", "deploy now"),
        ("name\tvalue", "name value"),
        ("a\r\nb\u2028c\u2029d\u0085e\vf\fg", "a b c d e f g"),
        # Cc and Cf are removed: what is left can't move the cursor or reorder the line.
        ("\x1b[2J\x1b[1;1Hok", "[2J[1;1Hok"),
        ("\x9b31mred", "31mred"),
        ("invoice \u202egpj.exe", "invoice gpj.exe"),
        ("pay\u200bpal", "paypal"),
        ("bell\x07\x00", "bell"),
        # Spaces collapse and the ends are trimmed.
        ("   one  \n\n  two   ", "one two"),
    ],
)
def test_preview_is_one_line_with_no_controls(text: str, shown: str) -> None:
    assert preview(text) == shown


def test_preview_keeps_80_code_points_after_collapsing() -> None:
    assert preview("\u00e9" * 100) == "\u00e9" * 80
    assert preview("a" + " " * 100 + "b" * 100) == "a " + "b" * 78
