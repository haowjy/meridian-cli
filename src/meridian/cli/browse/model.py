"""Pure state for the session browser's three views: list, detail and lineage."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from meridian.cli.browse.transcript import Line, TranscriptWindow, entry_lines
from meridian.lib.ops.session_lineage import SessionLineageEdge
from meridian.lib.ops.session_list import SessionListOutput, SessionListRow

type Status = Literal["active", "archived"]
type Sort = Literal["updated", "created"]
type Control = Literal["status", "sort"]
type SearchMode = Literal["off", "input", "running", "results"]

STATUSES: tuple[Status, ...] = ("active", "archived")
SORTS: tuple[Sort, ...] = ("updated", "created")


@dataclass
class ListState:
    listing: SessionListOutput
    status: Status
    sort: Sort
    filter_text: str = ""
    highlight: int = 0
    focus: Control | None = None
    search_mode: SearchMode = "off"
    search_query: str = ""
    search_scanned: int = 0
    search_total: int = 0
    search_unavailable: int = 0
    search_matches: frozenset[str] = frozenset()
    message: str | None = None
    # A reload is in flight; ``loaded`` is the scope the current listing answers.
    pending: bool = False
    loaded: tuple[Status, Sort, str] | None = None

    def __post_init__(self) -> None:
        self.loaded = self.scope

    @property
    def scope(self) -> tuple[Status, Sort, str]:
        """The query inputs: the session_list query is the only definition of a match."""
        return self.status, self.sort, self.filter_text.strip()

    @property
    def loading(self) -> bool:
        """The body would show another scope's rows (or none yet) until the reload lands."""
        return self.pending and (self.loaded != self.scope or not self.visible_rows)

    @property
    def visible_rows(self) -> tuple[SessionListRow, ...]:
        rows = self.listing.rows
        if self.search_mode == "results":
            rows = tuple(row for row in rows if row.chat_id in self.search_matches)
        return rows

    @property
    def highlighted(self) -> SessionListRow | None:
        rows = self.visible_rows
        if self.loading or not rows:
            return None
        return rows[min(self.highlight, len(rows) - 1)]

    def clamp(self) -> None:
        self.highlight = max(0, min(self.highlight, len(self.visible_rows) - 1))

    def move(self, delta: int) -> None:
        self.highlight += delta
        self.clamp()
        self.message = None

    def edit_filter(self, text: str) -> None:
        self.filter_text = text
        self.highlight = 0
        self.message = None

    def cycle_focus(self) -> None:
        order: tuple[Control | None, ...] = (None, "status", "sort")
        self.focus = order[(order.index(self.focus) + 1) % len(order)]

    def step_focused(self, delta: int) -> bool:
        """Change the focused control; return whether the query inputs changed."""
        if self.focus == "status":
            changed = STATUSES[(STATUSES.index(self.status) + delta) % len(STATUSES)]
            moved = changed != self.status
            self.status = changed
        elif self.focus == "sort":
            changed = SORTS[(SORTS.index(self.sort) + delta) % len(SORTS)]
            moved = changed != self.sort
            self.sort = changed
        else:
            return False
        return moved

    def begin_reload(self) -> None:
        """Query inputs changed: stop any transcript search, which belonged to the old scope."""
        self.pending = True
        self.leave_search()

    def replace_listing(self, listing: SessionListOutput) -> None:
        self.listing = listing
        self.loaded = self.scope
        self.pending = False
        self.highlight = 0
        self.message = None
        self.leave_search()

    def leave_search(self) -> None:
        self.search_mode = "off"
        self.search_query = ""
        self.search_scanned = self.search_total = self.search_unavailable = 0
        self.search_matches = frozenset()
        self.clamp()

    def set_model(self, chat_id: str, model: str) -> None:
        rows = tuple(
            row.model_copy(update={"model": model}) if row.chat_id == chat_id else row
            for row in self.listing.rows
        )
        self.listing = self.listing.model_copy(update={"rows": rows})


@dataclass
class DetailState:
    chat_id: str
    row: SessionListRow | None
    model: str = ""
    window: TranscriptWindow | None = None
    error: str | None = None
    # First visible line; ``None`` pins the newest line to the bottom of the body.
    top: int | None = None
    message: str | None = None
    _lines: dict[tuple[int, int], list[Line]] = field(default_factory=lambda: {})
    # Flattened lines for one (width, lo, hi); a repaint or scroll step reuses them. Loading
    # older entries always moves ``lo``.
    _flat: tuple[tuple[int, int, int], list[Line]] | None = None

    def lines(self, width: int) -> list[Line]:
        window = self.window
        if window is None:
            return []
        key = (width, window.lo, window.hi)
        if self._flat is not None and self._flat[0] == key:
            return self._flat[1]
        out: list[Line] = []
        for entry in window.entries:
            entry_key = (entry.index, width)
            if entry_key not in self._lines:
                self._lines[entry_key] = entry_lines(entry, width)
            out.extend(self._lines[entry_key])
        self._flat = (key, out)
        return out

    def first_line(self, width: int, height: int) -> int:
        count = len(self.lines(width))
        limit = max(0, count - height)
        return limit if self.top is None else max(0, min(self.top, limit))

    def scroll(self, delta: int, width: int, height: int) -> None:
        window = self.window
        if window is None:
            return
        top = self.first_line(width, height) + delta
        while top < 0 and window.has_older:
            before = len(self.lines(width))
            window.older()
            top += len(self.lines(width)) - before
        while delta > 0 and window.has_newer and top + height >= len(self.lines(width)):
            window.newer()
        limit = max(0, len(self.lines(width)) - height)
        top = max(0, min(top, limit))
        self.top = None if top >= limit and not window.has_newer else top

    def jump(self, *, to_top: bool) -> None:
        if self.window is None:
            return
        if to_top:
            self.window.jump_top()
            self.top = 0
        else:
            self.window.jump_bottom()
            self.top = None


@dataclass(frozen=True)
class LineageNode:
    chat_id: str
    prefix: str  # tree guides drawn before the node
    kind: str  # edge to its parent: "fork", "from" or "" for a root
    row: SessionListRow | None


@dataclass
class LineageState:
    origin: str
    nodes: tuple[LineageNode, ...] | None = None  # None while loading
    cursor: int = 0
    error: str | None = None

    def move(self, delta: int) -> None:
        if self.nodes:
            self.cursor = max(0, min(self.cursor + delta, len(self.nodes) - 1))

    @property
    def selected(self) -> LineageNode | None:
        return self.nodes[self.cursor] if self.nodes else None


def _number(chat_id: str) -> tuple[int, str]:
    digits = chat_id.lstrip("c")
    return (int(digits), chat_id) if digits.isdigit() else (0, chat_id)


def lineage_chat_ids(origin: str, edges: tuple[SessionLineageEdge, ...]) -> tuple[str, ...]:
    ids = (
        {origin} | {edge.child_chat_id for edge in edges} | {edge.parent_chat_id for edge in edges}
    )
    return tuple(sorted(ids, key=_number))


def build_lineage_nodes(
    origin: str,
    edges: tuple[SessionLineageEdge, ...],
    rows: dict[str, SessionListRow],
) -> tuple[LineageNode, ...]:
    """Flatten ancestry and descendants into drawn tree lines.

    A chat with several recorded parents (``--fork`` plus ``--from``) is drawn under each.
    """

    children: dict[str, list[tuple[str, str]]] = {}
    has_parent: set[str] = set()
    for edge in edges:
        children.setdefault(edge.parent_chat_id, []).append((edge.child_chat_id, edge.kind))
        has_parent.add(edge.child_chat_id)
    for kids in children.values():
        kids.sort(key=lambda item: _number(item[0]))
    members = lineage_chat_ids(origin, edges)
    roots = [chat for chat in members if chat not in has_parent] or [origin]

    nodes: list[LineageNode] = []

    def walk(chat: str, kind: str, guide: str, branch: str, path: frozenset[str]) -> None:
        nodes.append(LineageNode(chat, guide + branch, kind, rows.get(chat)))
        kids = [(c, k) for c, k in children.get(chat, []) if c not in path]
        for index, (child, child_kind) in enumerate(kids):
            last = index == len(kids) - 1
            extension = "" if not branch else ("   " if branch.startswith("└") else "│  ")
            walk(
                child,
                child_kind,
                guide + extension,
                "└─ " if last else "├─ ",
                path | {chat},
            )

    for root in roots:
        walk(root, "", "", "", frozenset())
    return tuple(nodes)
