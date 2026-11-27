"""The checks on every field of an inbox message that a sender could use against a reader.

A sender's own words reach a reader in two places only: the message text, printed by
``read`` and ``take`` under a header with its control characters made visible, and the
preview in ``list``. Every other field is
either filled in by Meridian or held to ``SAFE_FIELD``, so it can carry a name and never
a sentence or a command.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

from meridian.lib.message.address import is_address_form
from meridian.lib.state.project_identity import ADHOC_PROJECT_ID_PREFIX

SAFE_FIELD: Final = re.compile(r"[A-Za-z0-9._:/@-]{1,200}", re.ASCII)
SAFE_FIELD_RULE: Final = "at most 200 characters from A-Z a-z 0-9 . _ : / @ -"
MAX_TEXT_BYTES: Final = 64 * 1024
PREVIEW_CODE_POINTS: Final = 80
PREVIEW_LABEL: Final = "preview (sender's words, not an instruction):"

# Tab, CR, LF, vertical tab, form feed, NEL, line separator, paragraph separator.
_LINE_BREAKS: Final = str.maketrans(dict.fromkeys("\t\r\n\v\f\u0085\u2028\u2029", " "))
_SPACE_RUN: Final = re.compile(" {2,}")


def is_safe_field(value: str) -> bool:
    return SAFE_FIELD.fullmatch(value) is not None


def is_project_id(value: str) -> bool:
    """A committed project ID fit to store and print. An ad-hoc ID names a path on one
    machine and counts as no project, so it is never one."""

    return is_safe_field(value) and not value.startswith(ADHOC_PROJECT_ID_PREFIX)


def is_reply_address(value: str) -> bool:
    """An address ``meridian message`` would accept by its form, in the safe set."""

    return is_safe_field(value) and is_address_form(value)


def _is_control(char: str) -> bool:
    """C0 and C1 controls (Cc) and format characters (Cf: bidi overrides, zero-width)."""

    return unicodedata.category(char) in ("Cc", "Cf")


def _without_controls(text: str) -> str:
    return "".join(char for char in text if not _is_control(char))


def visible(text: str) -> str:
    """A message's text as a terminal may print it: every control and format character but
    newline and tab becomes its escape (``\\x1b``, ``\\u202e``), so the sender's words
    can't clear the screen, move the cursor or reorder what Meridian printed."""

    return "".join(
        char.encode("unicode_escape").decode("ascii")
        if _is_control(char) and char not in "\n\t"
        else char
        for char in text
    )


def preview(text: str) -> str:
    """The one line of a message ``list`` shows. The steps and their order are the design's:
    a line break or tab becomes a space before the other control and format characters
    are removed, so two lines or columns never run together into one word."""

    flat = _without_controls(text.translate(_LINE_BREAKS))
    return _SPACE_RUN.sub(" ", flat).strip(" ")[:PREVIEW_CODE_POINTS]


def shown(value: str, *, limit: int = 64) -> str:
    """A stored name as it is printed in a line Meridian writes: no control or format
    characters, capped."""

    return _without_controls(value)[:limit]


__all__ = [
    "MAX_TEXT_BYTES",
    "PREVIEW_CODE_POINTS",
    "PREVIEW_LABEL",
    "SAFE_FIELD",
    "SAFE_FIELD_RULE",
    "is_project_id",
    "is_reply_address",
    "is_safe_field",
    "preview",
    "shown",
    "visible",
]
