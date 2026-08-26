"""Pure prompt_toolkit formatted-text rendering for session browse."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prompt_toolkit.formatted_text import StyleAndTextTuples

from meridian.cli.browse.model import BrowseModel
from meridian.lib.core.formatting import relative_time
from meridian.lib.ops.session_reentry import Blocked, Fork, SessionReentryDecision


def _clip(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(value) <= width:
        return value
    if width == 1:
        return "…"
    return value[: width - 1].rstrip() + "…"


def render_status(model: BrowseModel, width: int) -> StyleAndTextTuples:
    if model.mode in {"search-input", "searching"}:
        label = f"search: {model.search_query}"
        if model.mode == "searching":
            if model.search_matches is None:
                progress = f"scanned {model.search_scanned}/{model.search_total}"
            else:
                progress = f"{len(model.search_matches)} of {model.search_total} match"
            label = f"{label}  {progress}"
    else:
        label = f"filter: {model.filter_text}"
    return [("class:status", _clip(label, width))]


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
        if width < 60:
            line = f"{marker} {live} {row.chat_id:<5} {age:<4} {row.work_label or '—'}"
        else:
            line = (
                f"{marker} {live} {row.chat_id:<5} {age:<4} "
                f"{(row.agent or '—'):<10} {(row.model or '—'):<10} {row.work_label or '—'}"
            )
        style = "class:selected" if selected else "class:row"
        fragments.append((style, _clip(line, width)))
        if row.live:
            fragments.append(("class:live", ""))
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
    elif model.mode == "searching" and model.search_matches is None:
        parts = ("searching transcripts", action, "[esc] cancel")
        text = " · ".join(part for part in parts if part)
    elif model.mode == "searching":
        text = " · ".join(part for part in ("search results", action, "[esc] back") if part)
    elif row is None:
        text = "[q]/[esc] quit"
    elif isinstance(row.reentry, Blocked):
        text = f"{action} · [esc] quit"
    else:
        text = f"type to filter · / search · {action} · [esc] quit"
    return [("class:footer", _clip(text, width))]


__all__ = ["render_footer", "render_list", "render_preview", "render_status"]
