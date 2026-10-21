"""Width-aware prompt_toolkit formatted text for each browse view (pure functions)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, NamedTuple

from meridian.cli.browse.model import (
    SORTS,
    STATUSES,
    DetailState,
    LineageNode,
    LineageState,
    ListState,
)
from meridian.cli.browse.text import cell, clip
from meridian.lib.core.formatting import relative_time
from meridian.lib.ops.session_list import SessionListRow

if TYPE_CHECKING:
    from prompt_toolkit.formatted_text import StyleAndTextTuples

_AGE_WIDTH = 4
_GAP = "  "
LIST_HEADER_HEIGHT = 3
DETAIL_HEADER_HEIGHT = 5


def _age(row: SessionListRow) -> str:
    return relative_time(row.last_turn_at or row.activity_at).removesuffix(" ago")


def _status_glyph(row: SessionListRow) -> tuple[str, str]:
    if row.stale:
        return "⚠", "class:stale"
    return " ", ""


def _lines(lines: list[StyleAndTextTuples]) -> StyleAndTextTuples:
    out: StyleAndTextTuples = []
    for index, line in enumerate(lines):
        if index:
            out.append(("", "\n"))
        out.extend(line)
    return out


def _title(value: str) -> str:
    return value.capitalize()


def _choice(options: tuple[str, ...], current: str, focused: bool) -> StyleAndTextTuples:
    out: StyleAndTextTuples = []
    for option in options:
        selected = option == current
        style = "class:choice-focus" if selected and focused else "bold" if selected else ""
        out += [(style, f"[{_title(option)}]" if selected else _title(option)), ("", " ")]
    return out


# --- list ---------------------------------------------------------------------------------


class _Layout(NamedTuple):
    tmux: int
    agent: int  # 0 when the AGENT column is dropped
    with_model: bool
    work: int  # WORK is last and takes whatever width remains


_WORK_MIN = 6


def _fit(rows: tuple[SessionListRow, ...], budget: int) -> _Layout:
    """Lay out TMUX, AGENT, AGE and WORK in ``budget`` columns.

    Narrow terminals shrink WORK first, then drop the ``(model)`` part, then AGENT.
    """

    def natural(values: list[str], low: int, high: int) -> int:
        return max(low, min(high, max([0, *map(len, values)])))

    tmux = natural([r.tmux_target or "" for r in rows], 4, 20)
    full = natural([r.agent_label() for r in rows], 5, 34)
    short = natural([r.agent_label(with_model=False) for r in rows], 5, 20)

    def used(tmux: int, agent: int) -> int:
        return tmux + len(_GAP) + (agent + len(_GAP) if agent else 0) + _AGE_WIDTH + len(_GAP)

    for agent, with_model in ((full, True), (short, False)):
        if budget - used(tmux, agent) >= _WORK_MIN:
            return _Layout(tmux, agent, with_model, budget - used(tmux, agent))
    tmux = max(4, tmux - max(0, _WORK_MIN - (budget - used(tmux, 0))))
    return _Layout(tmux, 0, False, max(0, budget - used(tmux, 0)))


def _cells(row: SessionListRow, layout: _Layout) -> str:
    """TMUX, AGENT, AGE and WORK cells of one row."""

    parts = [cell(row.tmux_target or "", layout.tmux)]
    if layout.agent:
        parts.append(cell(row.agent_label(with_model=layout.with_model), layout.agent))
    parts.append(cell(_age(row), _AGE_WIDTH, right=True))
    parts.append(clip(row.work_label, layout.work))
    return _GAP.join(parts)


def _cell_headers(layout: _Layout) -> str:
    parts = [cell("TMUX", layout.tmux)]
    if layout.agent:
        parts.append(cell("AGENT", layout.agent))
    parts.append(cell("AGE", _AGE_WIDTH, right=True))
    parts.append(clip("WORK", layout.work))
    return _GAP.join(parts)


def list_header(state: ListState, width: int) -> StyleAndTextTuples:
    count = f"{len(state.visible_rows)} of {state.listing.total_count}"
    narrow = width < 56  # drop padding so both controls still fit at 50 columns
    left: StyleAndTextTuples = [("", "Status: " if narrow else " Status: ")]
    left += _choice(STATUSES, state.status, state.focus == "status")
    left += [("", "Sort: " if narrow else " Sort: ")]
    left += _choice(SORTS, state.sort, state.focus == "sort")
    used = sum(len(fragment[1]) for fragment in left)
    status_line: StyleAndTextTuples = list(left)
    if width - used >= len(count) + 2:
        status_line.append(("", " " * (width - used - len(count) - 1)))
        status_line.append(("class:hint", count))

    if state.search_mode == "off":
        if state.filter_text:
            search: StyleAndTextTuples = [("bold", f"> {clip(state.filter_text, width - 2)}")]
        else:
            search = [("class:hint", "> type to search")]
    else:
        label = f"/ {state.search_query}"
        if state.search_mode == "running":
            label += f"   scanned {state.search_scanned}/{state.search_total}"
        elif state.search_mode == "results":
            label += f"   {len(state.search_matches)} of {state.search_total} match"
            if state.search_unavailable:
                label += f", {state.search_unavailable} unavailable"
        search = [("bold", clip(label, width))]

    column_line = "    " + _cell_headers(_fit(state.visible_rows, width - 4))
    return _lines([status_line, search, [("class:columns", clip(column_line, width))]])


def list_body(state: ListState, width: int) -> StyleAndTextTuples:
    rows = state.visible_rows
    if not rows:
        filtered = state.filter_text or state.search_mode == "results"
        empty = "no archived sessions" if state.status == "archived" else "no primary sessions"
        return [("class:hint", "  no matches" if filtered else f"  {empty}")]
    layout = _fit(rows, width - 4)
    out: StyleAndTextTuples = []
    for index, row in enumerate(rows):
        selected = index == state.highlight
        base = "class:selected" if selected else ""
        if selected:
            out.append(("[SetCursorPosition]", ""))
        glyph, glyph_style = _status_glyph(row)
        text = _cells(row, layout)
        out.append((base, "▸ " if selected else "  "))
        out.append((f"{base} {glyph_style}".strip(), glyph))
        out.append((base, f" {text}".ljust(max(0, width - 3))))
        if index < len(rows) - 1:
            out.append(("", "\n"))
    return out


def list_footer(state: ListState, width: int) -> StyleAndTextTuples:
    if state.message:
        return [("class:error", clip(state.message, width))]
    if state.search_mode == "input":
        hint = "enter search transcripts  esc cancel"
    elif state.search_mode != "off":
        hint = "enter resume  space view  esc back"
    elif state.focus is not None:
        hint = "←/→ change  tab next  esc done"
    else:
        lineage = "ctrl-l" if state.filter_text else "l"  # `l` is text once a filter has begun
        hint = f"enter resume  space view  {lineage} lineage  tab filters  / transcripts  esc quit"
    return [("class:hint", clip(hint, width))]


# --- detail -------------------------------------------------------------------------------


def _stamp(iso: str) -> str:
    try:
        local = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return iso or "-"
    return f"{local:%Y-%m-%d %H:%M} ({relative_time(iso)})"


def detail_header(state: DetailState, width: int) -> StyleAndTextTuples:
    row = state.row
    harness = row.harness if row is not None else ""
    tmux = (row.tmux_target if row else None) or "-"
    work = (row.work_label if row else None) or "-"
    agent = (row.agent if row else "") or "-"
    model = state.model or (row.model if row else "") or "-"
    created = _stamp(row.created_at) if row is not None and row.created_at else "-"
    last = relative_time(row.last_turn_at or row.activity_at) if row is not None else "-"
    window = state.window
    shown = (
        f" entries {window.lo + 1}-{window.hi} of {window.total}"
        if window is not None and window.entries
        else ""
    )

    def pair(*items: tuple[str, str]) -> StyleAndTextTuples:
        out: StyleAndTextTuples = []
        for label, value in items:
            out += [("class:label", f" {label} "), ("", value + " ")]
        return out

    lines: list[StyleAndTextTuples] = [
        [("bold", f" {state.chat_id}"), ("", f"  {harness}")],
        pair(("tmux", tmux), ("work", work)),
        pair(("agent", agent), ("model", model)),
        pair(("created", created), ("last turn", last)),
        [("class:hint", clip(f"──{shown} ".ljust(width, "─"), width))],
    ]
    return _lines(lines)


def detail_body(state: DetailState, width: int, height: int) -> StyleAndTextTuples:
    if state.error:
        return [("class:error", f" {clip(state.error, width - 2)}")]
    if state.window is None:
        return [("class:hint", " loading transcript…")]
    lines = state.lines(width)
    if not lines:
        return [("class:hint", " no transcript entries")]
    first = state.first_line(width, height)
    visible = lines[first : first + height]
    blank: StyleAndTextTuples = [("", "")]
    padded: list[StyleAndTextTuples] = [blank] * (height - len(visible))
    for style, text in visible:
        padded.append([(style, text)])
    return _lines(padded)


def detail_footer(state: DetailState, width: int) -> StyleAndTextTuples:
    if state.message:
        return [("class:error", clip(state.message, width))]
    hint = "↑/↓ pgup/pgdn home/end scroll  enter resume  l lineage  esc back"
    return [("class:hint", clip(hint, width))]


# --- lineage ------------------------------------------------------------------------------


_EDGE_WIDTH = 4


def _lineage_layout(nodes: tuple[LineageNode, ...], width: int) -> tuple[int, _Layout]:
    """Width of the CHAT tree cell and the shared TMUX/AGENT/AGE/WORK layout."""

    rows = tuple(n.row for n in nodes if n.row is not None)
    tree = max([len("CHAT"), *(len(n.prefix) + len(n.chat_id) for n in nodes)])
    # leading column, gutter, then CHAT and EDGE with their gaps
    prefix = 4 + tree + len(_GAP) + _EDGE_WIDTH + len(_GAP)
    return tree, _fit(rows, width - prefix)


def lineage_header(state: LineageState, width: int) -> StyleAndTextTuples:
    columns: StyleAndTextTuples = []
    if state.nodes:
        tree, layout = _lineage_layout(state.nodes, width)
        text = _GAP.join(
            ["    " + cell("CHAT", tree), cell("EDGE", _EDGE_WIDTH), _cell_headers(layout)]
        )
        columns = [("class:columns", clip(text, width))]
    return _lines(
        [
            [("bold", clip(f" Lineage of {state.origin}", width))],
            [("class:hint", clip(" forks and --from sources, ancestors first", width))],
            columns,
        ]
    )


def lineage_body(state: LineageState, width: int) -> StyleAndTextTuples:
    if state.error:
        return [("class:error", f" {clip(state.error, width - 2)}")]
    if state.nodes is None:
        return [("class:hint", " loading lineage…")]
    tree, layout = _lineage_layout(state.nodes, width)
    out: StyleAndTextTuples = []
    for index, node in enumerate(state.nodes):
        selected = index == state.cursor
        base = "class:selected" if selected else ""
        if selected:
            out.append(("[SetCursorPosition]", ""))
        row = node.row
        glyph, glyph_style = _status_glyph(row) if row is not None else (" ", "")
        label = cell(node.prefix + node.chat_id, tree)
        edge = cell(node.kind, _EDGE_WIDTH)
        rest = "(not in this project's index)" if row is None else _cells(row, layout)
        weight = "bold" if node.chat_id == state.origin else ""
        out.append((base, "▸ " if selected else "  "))
        out.append((f"{base} {glyph_style}".strip(), glyph))
        text = f" {label}{_GAP}{edge}{_GAP}{rest}"
        out.append((f"{base} {weight}".strip(), clip(text, width - 3).ljust(width - 3)))
        if index < len(state.nodes) - 1:
            out.append(("", "\n"))
    return out


def lineage_footer(state: LineageState, width: int) -> StyleAndTextTuples:
    return [("class:hint", clip("↑/↓ move  enter open detail  esc back", width))]
