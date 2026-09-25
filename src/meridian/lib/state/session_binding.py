"""One locked native bind path, amortizing journal replay for bulk imports."""

import json
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from meridian.lib.core.native_identity import BindSource
from meridian.lib.core.types import ChatId, HarnessSessionId
from meridian.lib.platform.locking import lock_file
from meridian.lib.state import session_store as sessions
from meridian.lib.state.atomic import append_durable_jsonl_line
from meridian.lib.state.event_store import read_events
from meridian.lib.state.history_changes import HistoryChanges, HistorySource
from meridian.lib.state.native_binding import Bound, Conflict, bind, report_conflict
from meridian.lib.state.paths import RuntimePaths


class SessionBindings:
    """Lock-scoped authority snapshot; construct only through session_bindings."""

    def __init__(self, runtime_root: Path) -> None:
        self.runtime_root = runtime_root
        self.paths = RuntimePaths.from_root_dir(runtime_root)
        self.events = read_events(self.paths.sessions_jsonl, sessions._parse_event)
        self._pending: list[sessions.SessionUpdateEvent] = []
        self.records: dict[str, sessions.SessionRecord] = {}
        self.historical: set[tuple[str, str]] = set()
        for event in self.events:
            sessions.project_session_event(self.records, event)
            if isinstance(event, sessions.SessionHistoricalEvent):
                self.historical.add((event.chat_id, event.session_instance_id))

    def bind(
        self,
        chat_id: str,
        harness_session_id: str,
        *,
        native_store: str | None = None,
        source: BindSource = "observed",
        session_instance_id: str | None = None,
        startup_attempt_id: str | None = None,
    ) -> sessions.NativeBindingResult:
        if startup_attempt_id is not None and session_instance_id is None:
            raise ValueError("startup identity requires a captured session generation")
        existing = self.records.get(chat_id)
        if existing is None:
            raise ValueError(f"Unknown chat: {chat_id}")
        event = sessions.SessionUpdateEvent(
            chat_id=ChatId(chat_id),
            harness_session_id=HarnessSessionId(harness_session_id),
            native_store=native_store,
            source=source,
            session_instance_id=(
                session_instance_id
                if session_instance_id is not None
                else sessions._session_instance_for_event(self.paths, self.runtime_root, chat_id)
            ),
            startup_attempt_id=startup_attempt_id,
        )
        if not sessions._generation_matches(
            existing.session_instance_id, event.session_instance_id
        ):
            return sessions.NativeBindingResult(
                "conflict", existing.harness_session_id, existing.native_store
            )
        outcome = bind(existing.key_fields(), event.key_fields())
        if isinstance(outcome, Conflict):
            report_conflict(chat_id, outcome, source)
            return sessions.NativeBindingResult(
                "conflict", existing.harness_session_id, existing.native_store
            )
        status = "bound" if isinstance(outcome, Bound) else "already_bound"
        if startup_attempt_id is not None:
            sessions._validate_startup_identity(self.events, event)
        if status == "bound" or startup_attempt_id is not None:
            if (event.chat_id, event.session_instance_id) in self.historical:
                raise ValueError("Historical sessions are inert and cannot be mutated")
            self._pending.append(event)
            self.events.append(event)
            sessions.project_session_event(self.records, event)
        return sessions.NativeBindingResult(
            status,
            outcome.key.session_id,
            outcome.key.native_store,
        )

    def commit(self) -> None:
        if not self._pending:
            return
        HistoryChanges(self.runtime_root).mark(HistorySource(kind="sessions"))
        lines = "".join(
            json.dumps(
                event.model_dump(mode="json", exclude_none=True),
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
            for event in self._pending
        )
        # One fsync per lock-scoped batch; torn tails remain recoverable JSONL.
        append_durable_jsonl_line(self.paths.sessions_jsonl, lines)
        self._pending.clear()


@contextmanager
def session_bindings(runtime_root: Path) -> Generator[SessionBindings]:
    """Keep reads and appends in one sessions-lock epoch, including bulk imports."""
    paths = RuntimePaths.from_root_dir(runtime_root)
    with (
        lock_file(HistoryChanges(runtime_root).mutation_lock, mode="shared"),
        lock_file(paths.sessions_flock),
    ):
        bindings = SessionBindings(runtime_root)
        yield bindings
        bindings.commit()
