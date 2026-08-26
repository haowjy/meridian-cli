"""Pure prompt_toolkit formatted-text rendering for session browse."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prompt_toolkit.formatted_text import StyleAndTextTuples

from prompt_toolkit.utils import get_cwidth

from meridian.cli.browse.model import BrowseModel
from meridian.lib.core.formatting import relative_time
from meridian.lib.ops.session_reentry import Blocked, Fork, SessionReentryDecision

_WIDE_LIST_WIDTH = 60
_CHAT_ID_WIDTH = 7
_AGE_WIDTH = 4
_AGENT_WIDTH = 12
_MODEL_WIDTH = 16


def _clip(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if get_cwidth(value) <= width:
        return value
    if width == 1:
        return "…"
    target_width = width - get_cwidth("…")
    clipped: list[str] = []
    used_width = 0
    for character in value:
        character_width = get_cwidth(character)
        if used_width + character_width > target_width:
            break
        clipped.append(character)
        used_width += character_width
    return "".join(clipped).rstrip() + "…"


def _cell(value: str, width: int) -> str:
    clipped = _clip(value, width)
    return clipped + " " * max(0, width - get_cwidth(clipped))


def render_status(model: BrowseModel, width: int) -> StyleAndTextTuples:
    if model.mode in {"search-input", "searching", "search-results"}:
        label = f"search: {model.search_query}"
        if model.mode == "searching":
            progress = f"scanned {model.search_scanned}/{model.search_total}"
            label = f"{label}  {progress}"
        elif model.mode == "search-results":
            progress = f"{len(model.search_matches)} of {model.search_total} match"
            label = f"{label}  {progress}"
    else:
        label = f"filter: {model.filter_text}"
    return [("class:status", _clip(label, width))]


def render_list_header(model: BrowseModel, width: int) -> StyleAndTextTuples:
    if not model.visible_rows:
        return []
    if width < _WIDE_LIST_WIDTH:
        line = f"    {_cell('C-ID', _CHAT_ID_WIDTH)} {_cell('AGE', _AGE_WIDTH)} WORK"
    else:
        line = (
            f"    {_cell('C-ID', _CHAT_ID_WIDTH)} {_cell('AGE', _AGE_WIDTH)} "
            f"{_cell('AGENT', _AGENT_WIDTH)} {_cell('MODEL', _MODEL_WIDTH)} WORK"
        )
    return [("class:list-header", _clip(line, width))]


def render_list(model: BrowseModel, width: int) -> StyleAndTextTuples:
    rows = model.visible_rows
    if not rows:
        return [("class:empty", "no primary sessions")]

    fragments: StyleAndTextTuples = []
    for index, row in enumerate(rows):
        selected = index == model.highlight
        if selected:
            fragments.append(("[SetCursorPosition]", ""))
        marker = "▸" if selected else " "
        live = "●" if row.live else " "
        age = relative_time(row.activity_at).removesuffix(" ago")
        prefix = f"{marker} {live} "
        if width < _WIDE_LIST_WIDTH:
            line = (
                f"{prefix}{_cell(row.chat_id, _CHAT_ID_WIDTH)} "
                f"{_cell(age, _AGE_WIDTH)} {row.work_label or '—'}"
            )
        else:
            line = (
                f"{prefix}{_cell(row.chat_id, _CHAT_ID_WIDTH)} {_cell(age, _AGE_WIDTH)} "
                f"{_cell(row.agent or '—', _AGENT_WIDTH)} "
                f"{_cell(row.model or '—', _MODEL_WIDTH)} {row.work_label or '—'}"
            )
        style = "class:selected" if selected else "class:row"
        clipped_line = _clip(line, width)
        fragments.append((style, clipped_line[:2]))
        live_style = f"{style} class:live" if row.live else style
        fragments.append((live_style, clipped_line[2:3]))
        fragments.append((style, clipped_line[3:]))
        fragments.append(("", "\n"))
    if model.older_count:
        hint = f"+{model.older_count} older · raise --limit to see more"
        fragments.append(("class:hint", _clip(hint, width)))
    return fragments


def render_preview(model: BrowseModel, width: int, height: int) -> StyleAndTextTuples:
    row = model.highlighted_row
    if row is None:
        return [("class:empty", "")]
    if model.preview_loading or model.preview_chat_id != row.chat_id:
        lines = ("loading preview…",)
    else:
        lines = model.preview_lines or ("preview temporarily unavailable",)
    visible = lines[-max(1, height - 1) :]
    header = f"{row.chat_id} · current segment"
    fragments: StyleAndTextTuples = [("class:preview-title", _clip(header, width)), ("", "\n")]
    for line in visible:
        fragments.extend((("class:preview", _clip(line, width)), ("", "\n")))
    return fragments


def _action_hint(decision: SessionReentryDecision) -> str:
    if isinstance(decision, Fork):
        return "[enter] fork → new session (live)"
    if isinstance(decision, Blocked):
        return decision.reason
    return "[enter] resume"


def render_footer(model: BrowseModel, width: int) -> StyleAndTextTuples:
    if model.inline_message:
        escape_action = "quit" if model.mode == "list" else "back"
        text = f"{model.inline_message} · [esc] {escape_action}"
        return [("class:error", _clip(text, width))]
    row = model.highlighted_row
    action = _action_hint(row.reentry) if row is not None else ""
    if model.mode == "search-input":
        text = "type query · [enter] search · [esc] back"
    elif model.mode == "searching":
        parts = ("searching transcripts", action, "[esc] cancel")
        text = " · ".join(part for part in parts if part)
    elif model.mode == "search-results":
        text = " · ".join(part for part in ("search results", action, "[esc] back") if part)
    elif row is None:
        text = "[q]/[esc] quit"
    elif isinstance(row.reentry, Blocked):
        text = f"{action} · [esc] quit"
    else:
        text = f"type to filter · / search · {action} · [esc] quit"
    return [("class:footer", _clip(text, width))]


__all__ = [
    "render_footer",
    "render_list",
    "render_list_header",
    "render_preview",
    "render_status",
]
