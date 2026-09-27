"""One locked native bind path, amortizing journal replay for bulk imports."""

import json
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from meridian.lib.core.native_identity import BindSource, NativeKeyFields
from meridian.lib.core.types import ChatId, HarnessSessionId
from meridian.lib.platform.locking import lock_file
from meridian.lib.state import session_fold as sessions
from meridian.lib.state.atomic import append_durable_jsonl_line
from meridian.lib.state.event_store import read_events
from meridian.lib.state.history_changes import HistoryChanges, HistorySource
from meridian.lib.state.native_binding import BindOutcome, Bound, Conflict, bind, report_conflict
from meridian.lib.state.paths import RuntimePaths
from meridian.lib.state.session_store import resolve_session_instance_id


class SessionBindings:
    """Lock-scoped authority snapshot; construct only through session_bindings."""

    def __init__(self, runtime_root: Path) -> None:
        self.runtime_root = runtime_root
        self.paths = RuntimePaths.from_root_dir(runtime_root)
        self.events = read_events(self.paths.sessions_jsonl, sessions.parse_event)
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
        attempted: NativeKeyFields,
        *,
        source: BindSource,
        session_instance_id: str | None = None,
        startup_attempt_id: str | None = None,
    ) -> BindOutcome:
        if startup_attempt_id is not None and session_instance_id is None:
            raise ValueError("startup identity requires a captured session generation")
        existing = self.records.get(chat_id)
        if existing is None:
            raise ValueError(f"Unknown chat: {chat_id}")
        event = sessions.SessionUpdateEvent(
            chat_id=ChatId(chat_id),
            harness_session_id=(
                HarnessSessionId(attempted.session_id) if attempted.session_id else None
            ),
            native_store=attempted.native_store,
            source=source,
            session_instance_id=(
                session_instance_id
                if session_instance_id is not None
                else resolve_session_instance_id(self.paths, self.runtime_root, chat_id)
            ),
            startup_attempt_id=startup_attempt_id,
        )
        if not sessions.generation_matches(existing.session_instance_id, event.session_instance_id):
            return Conflict(existing.key_fields(), attempted, "generation")
        outcome = bind(existing.key_fields(), attempted)
        if isinstance(outcome, Conflict):
            report_conflict(chat_id, outcome, source)
            return outcome
        if startup_attempt_id is not None:
            sessions.validate_startup_identity(self.events, event)
        if isinstance(outcome, Bound) or startup_attempt_id is not None:
            if (event.chat_id, event.session_instance_id) in self.historical:
                raise ValueError("Historical sessions are inert and cannot be mutated")
            self._pending.append(event)
            self.events.append(event)
            sessions.project_session_event(self.records, event)
        return outcome

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
