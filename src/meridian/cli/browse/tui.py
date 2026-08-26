"""prompt_toolkit application and background lanes for session browse."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import partial
from queue import Empty, SimpleQueue
from typing import TYPE_CHECKING, Generic, TypeVar

from prompt_toolkit.application import Application
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

if TYPE_CHECKING:
    from prompt_toolkit.input import Input
    from prompt_toolkit.output import Output

from meridian.cli.browse.model import (
    Activate,
    Backspace,
    BrowseModel,
    Character,
    Enter,
    Escape,
    Interrupt,
    Key,
    Move,
    Quit,
    Search,
    StartSearch,
)
from meridian.cli.browse.render import (
    render_footer,
    render_list,
    render_preview,
    render_status,
)
from meridian.lib.ops.session_list import SessionListOutput
from meridian.lib.ops.session_reentry import Blocked, Fork, Resume, SessionReentryDecision

RequestT = TypeVar("RequestT")
ResultT = TypeVar("ResultT")
LaneWorker = Callable[
    [RequestT, Callable[[], bool], Callable[[ResultT], None]],
    None,
]


class Lane(Generic[RequestT, ResultT]):
    """Latest-only request slot with identity-based stale completion removal."""

    def __init__(self, worker: LaneWorker[RequestT, ResultT], invalidate: Callable[[], None]):
        self._worker = worker
        self._invalidate = invalidate
        self._slot: RequestT | None = None
        self._condition = threading.Condition()
        self._shutdown = False
        self._mailbox: SimpleQueue[tuple[RequestT, ResultT]] = SimpleQueue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def submit(self, request: RequestT) -> None:
        with self._condition:
            self._slot = request
            self._condition.notify()

    def clear(self) -> None:
        with self._condition:
            self._slot = None
            self._condition.notify()

    def is_current(self, request: RequestT) -> bool:
        with self._condition:
            return self._slot is request and not self._shutdown

    def drain(self) -> Iterator[ResultT]:
        while True:
            try:
                request, result = self._mailbox.get_nowait()
            except Empty:
                return
            if self.is_current(request):
                yield result

    def close(self) -> None:
        with self._condition:
            self._shutdown = True
            self._slot = None
            self._condition.notify()
        self._thread.join(timeout=1.0)

    def _run(self) -> None:
        processed: RequestT | None = None
        while True:
            with self._condition:
                while not self._shutdown and (
                    self._slot is None or self._slot is processed
                ):
                    self._condition.wait()
                if self._shutdown:
                    return
                request = self._slot
            assert request is not None
            current = partial(self.is_current, request)
            post = partial(self._post, request)
            self._worker(request, current, post)
            processed = request

    def _post(self, request: RequestT, result: ResultT) -> None:
        self._mailbox.put((request, result))
        self._invalidate()


@dataclass(frozen=True)
class PreviewRequest:
    chat_id: str


@dataclass(frozen=True)
class PreviewResult:
    chat_id: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class SearchRequest:
    query: str
    chat_ids: tuple[str, ...]


@dataclass(frozen=True)
class SearchProgress:
    scanned: int
    total: int


@dataclass(frozen=True)
class SearchDone:
    matched_chat_ids: frozenset[str]
    total: int


type SearchResult = SearchProgress | SearchDone


def _preview_worker(project_root: str) -> LaneWorker[PreviewRequest, PreviewResult]:
    def work(
        request: PreviewRequest,
        current: Callable[[], bool],
        post: Callable[[PreviewResult], None],
    ) -> None:
        from meridian.lib.ops.session_log import SessionLogInput, session_log_sync
        from meridian.lib.ops.session_log_render import render_entry

        try:
            output = session_log_sync(
                SessionLogInput(ref=request.chat_id, tail=10, project_root=project_root)
            )
            lines: list[str] = []
            if output.source and "spawn" in output.source.lower():
                lines.append(f"source: {output.source}")
            for entry in output.entries:
                rendered, _collapsed = render_entry(entry, clean=True, truncate=True)
                lines.extend(rendered)
            if not lines:
                lines.append("preview temporarily unavailable")
        except (ValueError, FileNotFoundError, OSError) as exc:
            lines = [str(exc) or "transcript not found"]
        if current():
            post(PreviewResult(request.chat_id, tuple(lines)))

    return work


def _search_worker(project_root: str) -> LaneWorker[SearchRequest, SearchResult]:
    def work(
        request: SearchRequest,
        current: Callable[[], bool],
        post: Callable[[SearchResult], None],
    ) -> None:
        from meridian.lib.ops.session_search import iter_session_subset_search

        matches: set[str] = set()
        total = len(request.chat_ids)
        for scanned, step in enumerate(
            iter_session_subset_search(
                project_root=project_root,
                chat_ids=request.chat_ids,
                query=request.query,
            ),
            start=1,
        ):
            if not current():
                return
            if step.matched:
                matches.add(step.chat_id)
            post(SearchProgress(scanned, total))
        if current():
            post(SearchDone(frozenset(matches), total))

    return work


class _BrowseController:
    def __init__(
        self,
        *,
        listing: SessionListOutput,
        project_root: str,
        resolve_reentry: Callable[[str], SessionReentryDecision],
    ) -> None:
        self.model = BrowseModel(listing.rows)
        self._resolve_reentry = resolve_reentry
        self._app: Application[SessionReentryDecision | None] | None = None
        self._preview_lane = Lane(_preview_worker(project_root), self.invalidate)
        self._search_lane = Lane(_search_worker(project_root), self.invalidate)
        self._preview_request: PreviewRequest | None = None
        self.interrupted = False
        self._request_preview()

    def bind_app(self, app: Application[SessionReentryDecision | None]) -> None:
        self._app = app
        app.invalidate()

    def invalidate(self) -> None:
        if self._app is not None:
            self._app.invalidate()

    def close(self) -> None:
        self._preview_lane.close()
        self._search_lane.close()

    def _request_preview(self) -> None:
        row = self.model.highlighted_row
        if row is None:
            self._preview_request = None
            self._preview_lane.clear()
            return
        if self._preview_request is not None and self._preview_request.chat_id == row.chat_id:
            return
        request = PreviewRequest(row.chat_id)
        self._preview_request = request
        self.model.preview_loading = True
        self._preview_lane.submit(request)

    def drain(self) -> None:
        for result in self._preview_lane.drain():
            self.model.apply_preview(result.chat_id, result.lines)
        for result in self._search_lane.drain():
            if isinstance(result, SearchProgress):
                self.model.apply_search_progress(result.scanned, result.total)
            else:
                self.model.apply_search_done(result.matched_chat_ids, result.total)
                self._preview_request = None
                self._request_preview()

    def handle(self, key: Key) -> None:
        app = self._app
        assert app is not None
        prior_mode = self.model.mode
        prior_chat = self.model.highlighted_row.chat_id if self.model.highlighted_row else None
        command = self.model.handle_key(key)
        if prior_mode != "list" and self.model.mode == "list":
            self._search_lane.clear()
        if isinstance(command, Quit):
            if command.exit_code == 130:
                self.interrupted = True
            app.exit(result=None)
            return
        if isinstance(command, StartSearch):
            self._search_lane.submit(SearchRequest(command.query, command.chat_ids))
        elif isinstance(command, Activate):
            decision = self._resolve_reentry(command.chat_id)
            if isinstance(decision, Blocked):
                self.model.apply_blocked(command.chat_id, decision.reason)
            else:
                app.exit(result=decision)
                return
        current_chat = self.model.highlighted_row.chat_id if self.model.highlighted_row else None
        if current_chat != prior_chat:
            self._preview_request = None
        self._request_preview()
        app.invalidate()


def run_browse_picker(
    listing: SessionListOutput,
    project_root: str,
    resolve_reentry: Callable[[str], SessionReentryDecision],
    *,
    input: Input | None = None,
    output: Output | None = None,
) -> Resume | Fork | None:
    """Run the full-screen picker and return only safe re-entry decisions."""

    app: Application[SessionReentryDecision | None]
    controller = _BrowseController(
        listing=listing,
        project_root=project_root,
        resolve_reentry=resolve_reentry,
    )

    def size() -> tuple[int, int]:
        dimensions = app.output.get_size()
        return dimensions.columns, dimensions.rows

    def status_text():
        controller.drain()
        width, _height = size()
        return render_status(controller.model, width)

    def list_text():
        controller.drain()
        width, _height = size()
        return render_list(controller.model, width)

    def preview_text():
        controller.drain()
        width, height = size()
        return render_preview(controller.model, width, max(1, height // 2 - 2))

    def footer_text():
        controller.drain()
        width, _height = size()
        return render_footer(controller.model, width)

    bindings = KeyBindings()

    @bindings.add("up")
    def _up(event: KeyPressEvent) -> None:
        _ = event
        controller.handle(Move(-1))

    @bindings.add("down")
    def _down(event: KeyPressEvent) -> None:
        _ = event
        controller.handle(Move(1))

    @bindings.add("enter")
    def _enter(event: KeyPressEvent) -> None:
        _ = event
        controller.handle(Enter())

    @bindings.add("backspace")
    def _backspace(event: KeyPressEvent) -> None:
        _ = event
        controller.handle(Backspace())

    @bindings.add("/")
    def _search(event: KeyPressEvent) -> None:
        _ = event
        controller.handle(Search())

    @bindings.add("escape")
    def _escape(event: KeyPressEvent) -> None:
        _ = event
        controller.handle(Escape())

    @bindings.add("c-c")
    def _interrupt(event: KeyPressEvent) -> None:
        _ = event
        controller.handle(Interrupt())

    @bindings.add(Keys.Any)
    def _character(event: KeyPressEvent) -> None:
        controller.handle(Character(event.data))

    list_window = Window(FormattedTextControl(list_text), wrap_lines=False)
    preview_window = ConditionalContainer(
        Window(FormattedTextControl(preview_text), wrap_lines=False),
        filter=Condition(lambda: size()[0] >= 60),
    )
    root = HSplit(
        (
            Window(FormattedTextControl(status_text), height=1),
            list_window,
            preview_window,
            Window(FormattedTextControl(footer_text), height=1),
        )
    )
    style = Style.from_dict(
        {
            "status": "bold",
            "selected": "reverse",
            "live": "fg:ansigreen",
            "preview-title": "bold",
            "footer": "fg:ansibrightblack",
            "error": "fg:ansired",
            "empty": "italic",
        }
    )
    app = Application(
        layout=Layout(root),
        key_bindings=bindings,
        full_screen=True,
        style=style,
        input=input,
        output=output,
    )
    controller.bind_app(app)
    try:
        decision = app.run()
    finally:
        controller.close()
    if controller.interrupted:
        raise SystemExit(130)
    if isinstance(decision, (Resume, Fork)):
        return decision
    return None


__all__ = ["Lane", "run_browse_picker"]
