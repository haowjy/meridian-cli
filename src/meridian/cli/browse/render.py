"""Width-aware prompt_toolkit formatted text for each browse view (pure functions)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

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
    if row.live:
        return "●", "class:live"
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


def _list_columns(rows: tuple[SessionListRow, ...], width: int) -> list[tuple[str, int]]:
    """Choose TMUX/WORK/AGENT/MODEL widths; narrow terminals drop MODEL, then AGENT."""

    def natural(header: str, values: list[str], low: int, high: int) -> int:
        return max(low, min(high, max([len(header), *map(len, values)])))

    cols = [
        ("TMUX", natural("TMUX", [r.tmux_target or "" for r in rows], 4, 20)),
        ("WORK", natural("WORK", [r.work_label for r in rows], 4, 30)),
        ("AGENT", natural("AGENT", [r.agent for r in rows], 5, 14)),
        ("MODEL", natural("MODEL", [r.model for r in rows], 5, 26)),
    ]
    fixed = 4 + _AGE_WIDTH  # gutter + status cell, then age

    def total() -> int:
        return fixed + sum(w + len(_GAP) for _, w in cols)

    for droppable in ("MODEL", "AGENT"):
        if total() > width:
            cols = [c for c in cols if c[0] != droppable]
    for name, floor in (("WORK", 6), ("TMUX", 4)):
        if total() > width:
            overflow = total() - width
            cols = [(n, max(floor, w - overflow) if n == name else w) for n, w in cols]
    return cols


def _column_value(row: SessionListRow, name: str) -> str:
    return {
        "TMUX": row.tmux_target or "",
        "WORK": row.work_label,
        "AGENT": row.agent,
        "MODEL": row.model,
    }[name]


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

    column_line = "    " + _GAP.join(
        cell(name, w) for name, w in _list_columns(state.visible_rows, width)
    )
    column_line += _GAP + cell("AGE", _AGE_WIDTH, right=True)
    return _lines([status_line, search, [("class:columns", clip(column_line, width))]])


def list_body(state: ListState, width: int) -> StyleAndTextTuples:
    rows = state.visible_rows
    if not rows:
        filtered = state.filter_text or state.search_mode == "results"
        empty = "no archived sessions" if state.status == "archived" else "no primary sessions"
        return [("class:hint", "  no matches" if filtered else f"  {empty}")]
    cols = _list_columns(rows, width)
    out: StyleAndTextTuples = []
    for index, row in enumerate(rows):
        selected = index == state.highlight
        base = "class:selected" if selected else ""
        if selected:
            out.append(("[SetCursorPosition]", ""))
        glyph, glyph_style = _status_glyph(row)
        text = _GAP.join(cell(_column_value(row, name), w) for name, w in cols)
        text += _GAP + cell(_age(row), _AGE_WIDTH, right=True)
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
    live = " ● live" if row is not None and row.live else ""
    harness = row.harness if row is not None else ""
    tmux = (row.tmux_target if row else None) or "-"
    work = (row.work_label if row else "") or "-"
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
        [("bold", f" {state.chat_id}"), ("", f"  {harness}"), ("class:live", live)],
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


def _lineage_layout(nodes: tuple[LineageNode, ...], width: int) -> tuple[int, int, int, int]:
    """Widths of the tree, TMUX, WORK and AGENT cells; WORK yields first on narrow terminals."""
    rows = [n.row for n in nodes if n.row is not None]
    tree = max([len("CHAT"), *(len(n.prefix) + len(n.chat_id) for n in nodes)])
    tmux = min(18, max([4, *(len(r.tmux_target or "") for r in rows)]))
    work = min(26, max([4, *(len(r.work_label) for r in rows)]))
    agent = min(12, max([5, *(len(r.agent) for r in rows)]))
    fixed = 4 + _EDGE_WIDTH + len(_GAP) * 5 + _AGE_WIDTH
    while fixed + tree + tmux + work + agent > width and work > 6:
        work -= 1
    return tree, tmux, work, agent


def lineage_header(state: LineageState, width: int) -> StyleAndTextTuples:
    columns: StyleAndTextTuples = []
    if state.nodes:
        tree, tmux, work, agent = _lineage_layout(state.nodes, width)
        text = _GAP.join(
            [
                "    " + cell("CHAT", tree),
                cell("EDGE", _EDGE_WIDTH),
                cell("TMUX", tmux),
                cell("WORK", work),
                cell("AGENT", agent),
                cell("AGE", _AGE_WIDTH, right=True),
            ]
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
    tree, tmux, work, agent = _lineage_layout(state.nodes, width)
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
        if row is None:
            rest = "(not in this project's index)"
        else:
            rest = _GAP.join(
                [
                    cell(row.tmux_target or "", tmux),
                    cell(row.work_label, work),
                    cell(row.agent, agent),
                    cell(_age(row), _AGE_WIDTH, right=True),
                ]
            )
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
