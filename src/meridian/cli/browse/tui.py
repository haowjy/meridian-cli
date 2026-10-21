"""prompt_toolkit application for session browse: list, transcript detail and lineage views."""

from __future__ import annotations

import contextlib
import importlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from prompt_toolkit.application import Application
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.styles import Style

from meridian.cli.browse import render
from meridian.cli.browse.lanes import Lanes, Post
from meridian.cli.browse.model import (
    DetailState,
    LineageState,
    ListState,
    Sort,
    Status,
    build_lineage_nodes,
    lineage_chat_ids,
)
from meridian.cli.browse.transcript import TranscriptWindow
from meridian.lib.ops.session_lineage import SessionLineageOutput
from meridian.lib.ops.session_list import SessionListInput, SessionListOutput, SessionListRow
from meridian.lib.ops.session_reentry import (
    Blocked,
    Fork,
    Launch,
    Resume,
    SessionReentryDecision,
    StartFromLaunch,
)

if TYPE_CHECKING:
    from prompt_toolkit.input import Input
    from prompt_toolkit.output import Output

_TYPING_DEBOUNCE = 0.15

type View = ListState | DetailState | LineageState


@dataclass
class StartPrompt:
    """Start-from's one-line prompt: the launch inputs fixed when it opened, and the text typed."""

    chat_id: str
    agent: str
    model: str
    work_id: str
    text: str = ""

    def type(self, value: str) -> None:
        self.text += value

    def paste(self, value: str) -> None:
        self.text += " ".join(value.split())

    def backspace(self) -> None:
        self.text = self.text[:-1]

    def launch(self) -> StartFromLaunch:
        return StartFromLaunch(
            self.chat_id, self.agent, self.model, self.work_id, self.text.strip()
        )


_STYLE = Style.from_dict(
    {
        "selected": "reverse",
        "stale": "fg:ansiyellow",
        "hint": "fg:ansibrightblack",
        "columns": "bold fg:ansibrightblack",
        "label": "fg:ansibrightblack",
        "error": "fg:ansired",
        "choice-focus": "reverse bold",
        "role-user": "bold fg:ansicyan",
        "role-assistant": "bold fg:ansigreen",
    }
)


def _preload(ready: threading.Event) -> None:
    """Import the detail, lineage and search ops once, off the UI thread.

    These modules import each other cyclically; importing them first from concurrent worker
    threads can observe a half-initialized package.
    """
    try:
        for name in (
            "session_lineage",
            "session_log",
            "session_model",
            "session_search",
            "session_transcript",
        ):
            importlib.import_module(f"meridian.lib.ops.{name}")
    finally:
        ready.set()


class _Browser:
    """Owns view navigation and every background request; state and drawing live elsewhere."""

    def __init__(
        self,
        *,
        listing: SessionListOutput,
        project_root: str,
        limit: int,
        status: Status,
        sort: Sort,
        resolve_reentry: Callable[[str], SessionReentryDecision],
    ) -> None:
        self.list = ListState(listing, status, sort)
        self.stack: list[DetailState | LineageState] = []
        self.interrupted = False
        self.prompt: StartPrompt | None = None
        self.app: Application[Launch | None] | None = None
        ready = threading.Event()
        self.lanes = Lanes(self._on_ui_loop, ready)
        threading.Thread(target=_preload, args=(ready,), daemon=True).start()
        self._project_root = project_root
        self._limit = limit
        self._resolve_reentry = resolve_reentry
        # One parsed snapshot survives Esc and re-open; a new generation reloads it.
        self._snapshot: tuple[str, str, TranscriptWindow] | None = None

    # --- plumbing ---------------------------------------------------------------------

    def _on_ui_loop(self, callback: Callable[[], None]) -> None:
        app = self.app
        if app is None or app.loop is None:
            return

        def run() -> None:
            callback()
            app.invalidate()

        with contextlib.suppress(RuntimeError):  # loop already closed during shutdown
            app.loop.call_soon_threadsafe(run)

    @property
    def view(self) -> View:
        return self.stack[-1] if self.stack else self.list

    def size(self) -> tuple[int, int]:
        assert self.app is not None
        dimensions = self.app.output.get_size()
        return dimensions.columns, dimensions.rows

    def header_height(self) -> int:
        if isinstance(self.view, DetailState):
            return render.DETAIL_HEADER_HEIGHT
        return render.LIST_HEADER_HEIGHT

    def body_height(self) -> int:
        return max(1, self.size()[1] - self.header_height() - 1)

    def exit(self, result: Launch | None) -> None:
        assert self.app is not None
        self.app.exit(result=result)

    # --- list -------------------------------------------------------------------------

    def requery(self, *, debounce: float = 0.0) -> None:
        """Reload the listing for the current status, sort and typed filter.

        Typing passes ``debounce`` so a burst of keystrokes costs one query; a newer request
        supersedes the sleeping one.
        """
        state = self.list
        state.begin_reload()
        self.lanes.cancel("search")  # a late search must not reinstate the old scope
        payload = SessionListInput(
            project_root=self._project_root,
            limit=self._limit,
            status=state.status,
            sort=state.sort,
            query=state.filter_text.strip(),
        )

        def work(current: Callable[[], bool], _post: Post) -> SessionListOutput:
            from meridian.lib.ops.session_list import session_list_sync

            if debounce:
                time.sleep(debounce)
                if not current():
                    return state.listing  # superseded; the lane drops this result
            return session_list_sync(payload)

        def fail(reason: str) -> None:
            state.pending = False
            state.message = f"could not load sessions: {reason}"

        self.lanes.run("list", work, state.replace_listing, fail)

    def start_search(self) -> None:
        state = self.list
        query = state.search_query.strip()
        if not query:
            return
        chat_ids = tuple(row.chat_id for row in state.visible_rows)  # what the Status view shows
        state.search_mode = "running"
        state.search_scanned, state.search_total, state.search_unavailable = 0, len(chat_ids), 0
        state.search_matches = frozenset()
        project_root = self._project_root

        def progress(scanned: int, unavailable: int) -> None:
            state.search_scanned, state.search_unavailable = scanned, unavailable

        def work(current: Callable[[], bool], post: Post) -> tuple[frozenset[str], int]:
            from meridian.lib.ops.session_search import iter_session_subset_search

            matches: set[str] = set()
            unavailable = 0
            steps = iter_session_subset_search(
                project_root=project_root, chat_ids=chat_ids, query=query
            )
            for scanned, step in enumerate(steps, start=1):
                if step.matched:
                    matches.add(step.chat_id)
                if step.error is not None:
                    unavailable += 1
                post(lambda s=scanned, u=unavailable: progress(s, u))
                if not current():
                    break
            return frozenset(matches), unavailable

        def done(result: tuple[frozenset[str], int]) -> None:
            state.search_matches, state.search_unavailable = result
            state.search_scanned = state.search_total
            state.search_mode = "results"
            state.highlight = 0
            state.clamp()

        def fail(reason: str) -> None:
            state.leave_search()
            state.message = f"search failed: {reason}"

        self.lanes.run("search", work, done, fail)

    # --- detail -----------------------------------------------------------------------

    def open_detail(self, chat_id: str, row: SessionListRow | None) -> None:
        detail = DetailState(chat_id, row, model=row.model if row else "")
        self.stack.append(detail)
        self._load_detail(detail)

    def _load_detail(self, detail: DetailState) -> None:
        generation = detail.row.session_generation if detail.row else ""
        snapshot = self._snapshot
        if snapshot is not None and snapshot[:2] == (detail.chat_id, generation):
            detail.window = snapshot[2]
        else:
            self._load_transcript(detail, generation)
        self._refresh_model(detail)

    def _load_transcript(self, detail: DetailState, generation: str) -> None:
        project_root = self._project_root

        def work(_current: Callable[[], bool], _post: Post) -> TranscriptWindow:
            return TranscriptWindow.load(detail.chat_id, project_root)

        def apply(window: TranscriptWindow) -> None:
            self._snapshot = (detail.chat_id, generation, window)
            detail.window = window

        def fail(reason: str) -> None:
            detail.error = f"transcript unavailable: {reason}"

        detail.error = None
        self.lanes.run("transcript", work, apply, fail)

    def _refresh_model(self, detail: DetailState) -> None:
        chat_id, project_root = detail.chat_id, self._project_root

        def work(_current: Callable[[], bool], _post: Post) -> str | None:
            from meridian.lib.ops.session_model import (
                SessionModelRefreshInput,
                session_model_refresh_sync,
            )

            return session_model_refresh_sync(
                SessionModelRefreshInput(chat_id=chat_id, project_root=project_root)
            ).model

        def apply(model: str | None) -> None:
            if model:
                detail.model = model
                self.list.set_model(chat_id, model)

        self.lanes.run("model", work, apply, lambda _reason: None)

    # --- lineage ----------------------------------------------------------------------

    def open_lineage(self, chat_id: str) -> None:
        state = LineageState(chat_id)
        self.stack.append(state)
        project_root, limit = self._project_root, self._limit

        def work(
            _current: Callable[[], bool], _post: Post
        ) -> tuple[SessionLineageOutput, dict[str, SessionListRow]]:
            from meridian.lib.ops.session_lineage import SessionLineageInput, session_lineage_sync
            from meridian.lib.ops.session_list import session_list_sync

            lineage = session_lineage_sync(
                SessionLineageInput(chat_id=chat_id, project_root=project_root)
            )
            ids = lineage_chat_ids(chat_id, lineage.edges)
            rows = session_list_sync(
                SessionListInput(
                    project_root=project_root, limit=max(limit, len(ids)), chat_ids=ids
                )
            ).rows
            return lineage, {row.chat_id: row for row in rows}

        def apply(result: tuple[SessionLineageOutput, dict[str, SessionListRow]]) -> None:
            lineage, rows = result
            nodes = build_lineage_nodes(chat_id, lineage.edges, rows)
            state.nodes = nodes
            state.cursor = next((i for i, n in enumerate(nodes) if n.chat_id == chat_id), 0)

        def fail(reason: str) -> None:
            state.error = f"lineage unavailable: {reason}"

        self.lanes.run("lineage", work, apply, fail)

    # --- navigation -------------------------------------------------------------------

    def back(self) -> None:
        popped = self.stack.pop()
        if isinstance(popped, DetailState):
            self.lanes.cancel("transcript")
            self.lanes.cancel("model")
        else:
            self.lanes.cancel("lineage")
        top = self.view
        if isinstance(top, DetailState) and top.window is None and top.error is None:
            self._load_detail(top)  # its load was superseded while a deeper view was open

    def chat_in_focus(self) -> tuple[str, SessionListRow | None] | None:
        view = self.view
        if isinstance(view, ListState):
            row = view.highlighted
            return (row.chat_id, row) if row is not None else None
        if isinstance(view, DetailState):
            return view.chat_id, view.row
        selected = view.selected
        return (selected.chat_id, selected.row) if selected is not None else None

    def activate(self) -> None:
        """Enter: resume or fork from list and detail; open a lineage node's detail."""
        view = self.view
        target = self.chat_in_focus()
        if target is None:
            return
        if isinstance(view, LineageState):
            self.open_detail(*target)
            return
        decision = self._resolve_reentry(target[0])
        if isinstance(decision, Blocked):
            view.message = f"{target[0]}: {decision.reason}"
        else:
            self.exit(decision)

    def begin_start_from(self) -> None:
        """`n`: open the prompt line for a fresh session that reads the focused chat."""
        view = self.view
        assert not isinstance(view, LineageState)  # `n` is bound only in list and detail
        target = self.chat_in_focus()
        if target is None:
            return
        chat_id, row = target
        if row is None:
            view.message = f"{chat_id}: not in this project's index"
            return
        if isinstance(row.start_from, Blocked):
            view.message = f"{chat_id}: {row.start_from.reason}"
            return
        # The model on screen: detail refreshes it, the list row is kept current by set_model.
        model = view.model if isinstance(view, DetailState) else row.model
        self.prompt = StartPrompt(chat_id, row.agent, model, row.work_label)

    def launch_start_from(self) -> None:
        assert self.prompt is not None
        self.exit(self.prompt.launch())

    def open_lineage_of_focus(self) -> None:
        target = self.chat_in_focus()
        if target is not None:
            self.open_lineage(target[0])


def _bindings(browser: _Browser) -> KeyBindings:
    bindings = KeyBindings()
    prompting = Condition(lambda: browser.prompt is not None)
    idle = ~prompting
    in_list = Condition(lambda: isinstance(browser.view, ListState)) & idle
    in_detail = Condition(lambda: isinstance(browser.view, DetailState)) & idle
    in_lineage = Condition(lambda: isinstance(browser.view, LineageState)) & idle
    searching_input = Condition(
        lambda: isinstance(browser.view, ListState) and browser.view.search_mode == "input"
    )

    def listing() -> ListState:
        assert isinstance(browser.view, ListState)
        return browser.view

    def detail() -> DetailState:
        assert isinstance(browser.view, DetailState)
        return browser.view

    def scroll(delta: int) -> None:
        detail().scroll(delta, browser.size()[0], browser.body_height())

    @bindings.add("c-c", eager=True)
    def _(_event: KeyPressEvent) -> None:
        browser.interrupted = True
        browser.exit(None)

    @bindings.add("enter", eager=True)
    def _(_event: KeyPressEvent) -> None:
        view = browser.view
        if browser.prompt is not None:
            browser.launch_start_from()
        elif isinstance(view, ListState) and view.search_mode == "input":
            browser.start_search()
        elif isinstance(view, ListState) and view.focus is not None:
            view.focus = None
        else:
            browser.activate()

    @bindings.add("escape", eager=True)
    def _(_event: KeyPressEvent) -> None:
        view = browser.view
        if browser.prompt is not None:
            browser.prompt = None
        elif not isinstance(view, ListState):
            browser.back()
        elif view.search_mode != "off":
            browser.lanes.cancel("search")
            view.leave_search()
        elif view.focus is not None:
            view.focus = None
        elif view.filter_text:
            view.edit_filter("")
            browser.requery()
        else:
            browser.exit(None)

    @bindings.add("c-l", filter=in_list | in_detail)
    def _(_event: KeyPressEvent) -> None:
        browser.open_lineage_of_focus()

    @bindings.add("c-n", filter=in_list | in_detail)
    def _(_event: KeyPressEvent) -> None:
        browser.begin_start_from()

    @bindings.add("n", filter=in_detail)
    def _(_event: KeyPressEvent) -> None:
        browser.begin_start_from()

    @bindings.add("backspace", filter=prompting)
    def _(_event: KeyPressEvent) -> None:
        assert browser.prompt is not None
        browser.prompt.backspace()

    @bindings.add(Keys.Any, filter=prompting)
    def _(event: KeyPressEvent) -> None:
        assert browser.prompt is not None
        if len(event.data) == 1 and event.data.isprintable():
            browser.prompt.type(event.data)

    @bindings.add(Keys.BracketedPaste, filter=prompting)
    def _(event: KeyPressEvent) -> None:
        assert browser.prompt is not None
        browser.prompt.paste(event.data)

    # list
    for key, delta in (("up", -1), ("down", 1), ("home", -(10**9)), ("end", 10**9)):

        @bindings.add(key, filter=in_list)
        def _(_event: KeyPressEvent, delta: int = delta) -> None:
            listing().move(delta)

    @bindings.add("pageup", filter=in_list)
    def _(_event: KeyPressEvent) -> None:
        listing().move(-browser.body_height())

    @bindings.add("pagedown", filter=in_list)
    def _(_event: KeyPressEvent) -> None:
        listing().move(browser.body_height())

    @bindings.add("tab", filter=in_list & ~searching_input)
    def _(_event: KeyPressEvent) -> None:
        listing().cycle_focus()

    for key, delta in (("left", -1), ("right", 1)):

        @bindings.add(key, filter=in_list)
        def _(_event: KeyPressEvent, delta: int = delta) -> None:
            if listing().step_focused(delta):
                browser.requery()

    @bindings.add("backspace", filter=in_list)
    def _(_event: KeyPressEvent) -> None:
        state = listing()
        if state.search_mode == "input":
            state.search_query = state.search_query[:-1]
        elif state.search_mode == "off":
            state.edit_filter(state.filter_text[:-1])
            browser.requery(debounce=_TYPING_DEBOUNCE)

    @bindings.add(Keys.Any, filter=in_list)
    def _(event: KeyPressEvent) -> None:
        state, value = listing(), event.data
        if len(value) != 1 or not value.isprintable() or state.search_mode == "running":
            return
        if state.search_mode == "input":
            state.search_query += value  # the only place a space is text
        elif value == " ":
            target = browser.chat_in_focus()
            if target is not None:
                browser.open_detail(*target)
        elif value == "l" and not state.filter_text:
            browser.open_lineage_of_focus()
        elif value == "n" and not state.filter_text:
            browser.begin_start_from()
        elif value == "/" and state.search_mode == "off":
            state.focus = None
            state.search_mode = "input"
            state.search_query = ""
        elif state.search_mode == "off":
            state.focus = None
            state.edit_filter(state.filter_text + value)
            browser.requery(debounce=_TYPING_DEBOUNCE)

    # detail
    for key, sign in (("up", -1), ("down", 1)):

        @bindings.add(key, filter=in_detail)
        def _(_event: KeyPressEvent, sign: int = sign) -> None:
            scroll(sign)

    @bindings.add("pageup", filter=in_detail)
    def _(_event: KeyPressEvent) -> None:
        scroll(-max(1, browser.body_height() - 1))

    @bindings.add("pagedown", filter=in_detail)
    def _(_event: KeyPressEvent) -> None:
        scroll(max(1, browser.body_height() - 1))

    @bindings.add("home", filter=in_detail)
    def _(_event: KeyPressEvent) -> None:
        detail().jump(to_top=True)

    @bindings.add("end", filter=in_detail)
    def _(_event: KeyPressEvent) -> None:
        detail().jump(to_top=False)

    @bindings.add("l", filter=in_detail)
    def _(_event: KeyPressEvent) -> None:
        browser.open_lineage_of_focus()

    # lineage
    for key, delta in (("up", -1), ("down", 1)):

        @bindings.add(key, filter=in_lineage)
        def _(_event: KeyPressEvent, delta: int = delta) -> None:
            assert isinstance(browser.view, LineageState)
            browser.view.move(delta)

    return bindings


def run_browse_picker(
    listing: SessionListOutput,
    project_root: str,
    resolve_reentry: Callable[[str], SessionReentryDecision],
    *,
    limit: int = 50,
    status: Status = "active",
    sort: Sort = "updated",
    input: Input | None = None,
    output: Output | None = None,
) -> Launch | None:
    """Run the full-screen picker and return only safe re-entry decisions."""

    browser = _Browser(
        listing=listing,
        project_root=project_root,
        limit=limit,
        status=status,
        sort=sort,
        resolve_reentry=resolve_reentry,
    )

    def header():
        view, (width, _) = browser.view, browser.size()
        if isinstance(view, ListState):
            return render.list_header(view, width)
        if isinstance(view, DetailState):
            return render.detail_header(view, width)
        return render.lineage_header(view, width)

    def body():
        view, (width, _) = browser.view, browser.size()
        if isinstance(view, ListState):
            return render.list_body(view, width)
        if isinstance(view, DetailState):
            return render.detail_body(view, width, browser.body_height())
        return render.lineage_body(view, width)

    def footer():
        view, (width, _) = browser.view, browser.size()
        if browser.prompt is not None:
            return render.start_prompt_footer(browser.prompt.chat_id, browser.prompt.text, width)
        if isinstance(view, ListState):
            return render.list_footer(view, width)
        if isinstance(view, DetailState):
            return render.detail_footer(view, width)
        return render.lineage_footer(view, width)

    root = HSplit(
        (
            Window(
                FormattedTextControl(header),
                height=lambda: Dimension.exact(browser.header_height()),
                wrap_lines=False,
            ),
            Window(FormattedTextControl(body), wrap_lines=False),
            Window(FormattedTextControl(footer), height=1, wrap_lines=False),
        )
    )
    app: Application[Launch | None] = Application(
        layout=Layout(root),
        key_bindings=_bindings(browser),
        full_screen=True,
        style=_STYLE,
        refresh_interval=30,  # age and stale markers are computed at render
        input=input,
        output=output,
    )
    app.ttimeoutlen = 0.05  # a lone Esc must not wait half a second for a possible sequence
    browser.app = app
    try:
        decision = app.run()
    finally:
        browser.lanes.close()
    if browser.interrupted:
        raise SystemExit(130)
    return decision if isinstance(decision, (Resume, Fork, StartFromLaunch)) else None


__all__ = ["run_browse_picker"]
