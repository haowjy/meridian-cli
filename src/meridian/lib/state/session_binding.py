"""One locked native bind path, amortizing journal replay for bulk imports."""

from collections.abc import Collection, Generator
from contextlib import contextmanager
from pathlib import Path

from meridian.lib.core.native_identity import BindSource, NativeKeyFields
from meridian.lib.core.types import ChatId, HarnessSessionId
from meridian.lib.state import session_fold as sessions
from meridian.lib.state.native_binding import BindOutcome, Bound, Conflict, bind, report_conflict
from meridian.lib.state.paths import RuntimePaths
from meridian.lib.state.session_store import (
    append_chat_rows,
    chats_rows,
    resolve_session_instance_id,
    session_writes,
)


class ChatOutsideBindingScope(RuntimeError):
    """A scoped bind snapshot was asked to bind a chat it never read."""

    def __init__(self, chat_id: str, scope: frozenset[str]) -> None:
        super().__init__(
            f"Chat {chat_id} is outside this session_bindings scope "
            f"({', '.join(sorted(scope))}); open session_bindings with that chat "
            "in scope, or without scope for a full snapshot."
        )
        self.chat_id = chat_id
        self.scope = scope


class SessionBindings:
    """Lock-scoped authority snapshot; construct only through session_bindings.

    ``scope`` is None for a full snapshot. Otherwise ``events`` and ``records``
    hold only the scoped chats' history, so cross-chat checks must require
    ``scope is None``, and binding any other chat raises
    ``ChatOutsideBindingScope``.
    """

    def __init__(self, runtime_root: Path, scope: Collection[str] | None = None) -> None:
        self.runtime_root = runtime_root
        self.paths = RuntimePaths.from_root_dir(runtime_root)
        self.scope = None if scope is None else frozenset(scope)
        self.events = chats_rows(runtime_root, self.scope, require_supported=True).ordered
        self._pending: list[sessions.SessionUpdateEvent] = []
        self.records: dict[str, sessions.SessionRecord] = {}
        for event in self.events:
            sessions.project_session_event(self.records, event)

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
        if self.scope is not None and chat_id not in self.scope:
            raise ChatOutsideBindingScope(chat_id, self.scope)
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
        if source in {"user_repair", "legacy_pi_recovery"}:
            if self.scope is not None:
                raise ValueError("Repair binds require a full session_bindings snapshot")
            for other_chat, other in self.records.items():
                if (
                    other_chat != chat_id
                    and attempted.session_id
                    and other.harness_session_id == attempted.session_id
                    and (source == "legacy_pi_recovery" or other.harness == attempted.harness)
                ):
                    return Conflict(other.key_fields(), attempted, "session_id")
        outcome = bind(existing.key_fields(), attempted)
        if isinstance(outcome, Conflict):
            report_conflict(chat_id, outcome, source)
            return outcome
        if startup_attempt_id is not None:
            sessions.validate_startup_identity(self.events, event)
        if isinstance(outcome, Bound) or startup_attempt_id is not None:
            if sessions.is_historical_generation(event.session_instance_id):
                raise ValueError("Historical sessions are inert and cannot be mutated")
            self._pending.append(event)
            self.events.append(event)
            sessions.project_session_event(self.records, event)
        return outcome

    def commit(self) -> None:
        if not self._pending:
            return
        batches: dict[str, list[sessions.SessionEvent]] = {}
        for event in self._pending:
            batches.setdefault(event.chat_id, []).append(event)
        append_chat_rows(self.runtime_root, batches)
        self._pending.clear()


@contextmanager
def session_bindings(
    runtime_root: Path, *, scope: Collection[str] | None = None
) -> Generator[SessionBindings]:
    """Keep reads and appends in one sessions-lock epoch, including bulk imports."""
    with session_writes(runtime_root):
        bindings = SessionBindings(runtime_root, scope)
        yield bindings
        bindings.commit()


def bind_native_key(
    root: Path,
    chat: str,
    attempted: NativeKeyFields,
    *,
    source: BindSource,
    session_instance_id: str | None = None,
    startup_attempt_id: str | None = None,
) -> BindOutcome:
    """Bind one chat under the same lock and validation as the batched writers."""
    scope = None if source in {"user_repair", "legacy_pi_recovery"} else {chat}
    with session_bindings(root, scope=scope) as bindings:
        return bindings.bind(
            chat,
            attempted,
            source=source,
            session_instance_id=session_instance_id,
            startup_attempt_id=startup_attempt_id,
        )
