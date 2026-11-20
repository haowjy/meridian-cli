"""The primary rows one ``agy`` process's MCP server holds, one thread per conversation.

A conversation becomes a Meridian primary row (harness ``agy``, the conversation ID as its
native session) when the server adopts it. The row has no runner PID: its launch lock, held
by this server for as long as the conversation's ``agy`` process lives, is its only
liveness, so a cancel or a recovery pass can never signal the person's TUI. The row ends
when the server closes its hold, or reads as crashed and is finished by recovery.

Each hold runs on its own thread because ``primary_hold`` blocks, and serves the row's
control socket on that thread's own event loop.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from meridian.lib.core.delivery import DeliveryKind, MessageRoute, MessageSender
from meridian.lib.harness.agy import inbox
from meridian.lib.harness.agy.state import (
    read_json,
    request_path,
    row_path,
    write_private_json,
)

if TYPE_CHECKING:
    from meridian.lib.launch.continue_launch import ContinueHold
    from meridian.lib.state.paths import LaunchPaths

logger = logging.getLogger(__name__)

_ADOPT_WAIT_SECONDS = 20.0
_CLOSE_WAIT_SECONDS = 0.3
_ROUTE_NOTE = "agy has no push channel: the message is read at the session's next model call."


@dataclass(frozen=True)
class HeldRow:
    """What a tool call needs to run as the conversation's session."""

    spawn_id: str
    chat_id: str
    project_root: Path
    runtime_root: Path
    cwd: Path


class _Reopen(NamedTuple):
    chat_id: str
    hold: ContinueHold


@dataclass
class _Hold:
    conversation: str
    ready: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)
    row: HeldRow | None = None
    thread: threading.Thread | None = None


class ConversationHolds:
    def __init__(self, proc_dir: Path) -> None:
        self._proc_dir = proc_dir
        self._holds: dict[str, _Hold] = {}
        self._lock = threading.Lock()

    def sync(self) -> None:
        """Adopt every primary conversation of this ``agy`` process that has no hold yet."""

        for path in sorted((self._proc_dir / "requests").glob("*.json")):
            self._adopt(path.stem)

    def row_for(self, conversation: str) -> HeldRow | None:
        """The row held for ``conversation``, adopting it first when its request just arrived."""

        hold = self._adopt(conversation)
        if hold is None:
            return None
        hold.ready.wait(_ADOPT_WAIT_SECONDS)
        return hold.row

    def close(self) -> None:
        """End every row now; whatever a kill leaves unfinished reads as crashed."""

        with self._lock:
            holds = list(self._holds.values())
        for hold in holds:
            hold.stop.set()
        deadline = time.monotonic() + _CLOSE_WAIT_SECONDS
        for hold in holds:
            if hold.thread is not None:
                hold.thread.join(max(0.0, deadline - time.monotonic()))

    def _adopt(self, conversation: str) -> _Hold | None:
        with self._lock:
            known = self._holds.get(conversation)
            if known is not None:
                return known
            request = read_json(request_path(self._proc_dir, conversation))
            if request is None or request.get("role") != "primary":
                return None
            hold = _Hold(conversation)
            self._holds[conversation] = hold
        hold.thread = threading.Thread(
            target=self._run,
            args=(hold, request),
            name=f"meridian-agy-hold-{conversation[:8]}",
            daemon=True,
        )
        hold.thread.start()
        return hold

    def _run(self, hold: _Hold, request: dict[str, Any]) -> None:
        try:
            _hold_row(hold, request, self._proc_dir)
        except Exception:
            logger.warning("agy conversation %s was not held", hold.conversation, exc_info=True)
        finally:
            hold.ready.set()


def _hold_row(hold: _Hold, request: dict[str, Any], proc_dir: Path) -> None:
    # Imported here: the hook shares this package and must not load the launch stack.
    from meridian.lib.bootstrap.services import build_spawn_application_service_from_roots
    from meridian.lib.config.project_root import resolve_project_root_resolution
    from meridian.lib.core.types import SpawnId
    from meridian.lib.launch.primary_hold import PrimaryRow, primary_hold
    from meridian.lib.launch.request import SessionRequest
    from meridian.lib.launch.types import PrimarySessionMetadata
    from meridian.lib.ops.runtime import resolve_runtime_root
    from meridian.lib.state import spawn_store

    conversation = hold.conversation
    cwd = Path(str(request.get("cwd") or Path.home()))
    project_root = resolve_project_root_resolution(execution_cwd=cwd, ignore_env=True).project_root
    runtime_root = resolve_runtime_root(project_root)
    service = build_spawn_application_service_from_roots(project_root, runtime_root)
    resumed = _reopen(project_root, runtime_root, conversation)
    row = PrimaryRow(
        spawn_id=(
            spawn_store.reserve_launch(runtime_root).spawn_id
            if resumed is None
            else SpawnId(resumed.hold.spawn.id)
        ),
        session_metadata=PrimarySessionMetadata(
            harness="agy",
            model=str(request.get("model") or ""),
            agent="",
            agent_path="",
            skills=(),
            skill_paths=(),
        ),
        session_request=SessionRequest(),
        prompt="Antigravity session",
        control_root=project_root,
        execution_cwd=cwd,
        resume_chat_id=resumed.chat_id if resumed else None,
        harness_session_id=conversation,
    )
    started = time.monotonic()
    with primary_hold(
        row, spawn_service=service, continue_hold=resumed.hold if resumed else None
    ) as primary:
        launch = primary.start(work_id=None)
        primary.bind_observed_identity(conversation, harness="agy", policy=None)
        service.lifecycle.bootstrap_from_disk(str(launch.spawn_id))
        hold.row = HeldRow(str(launch.spawn_id), primary.chat_id, project_root, runtime_root, cwd)
        write_private_json(
            row_path(proc_dir, conversation),
            {"spawn_id": hold.row.spawn_id, "chat_id": hold.row.chat_id},
        )
        hold.ready.set()
        try:
            asyncio.run(_serve(launch, conversation, hold.stop))
        finally:
            primary.complete(
                exit_code=0,
                extraction=None,
                started_monotonic=started,
                cancellation_observed=False,
                identity_error=None,
                launch_failure=None,
            )


def _reopen(project_root: Path, runtime_root: Path, conversation: str) -> _Reopen | None:
    """The chat this conversation already belongs to, and the hold to reopen its row with."""

    from meridian.lib.launch.continue_launch import begin_continue
    from meridian.lib.state import session_store, spawn_store
    from meridian.lib.state.session_identity import chat_owner_spawn

    chats = session_store.native_chats(runtime_root, conversation)
    if not chats:
        return None
    try:
        (chat_id,) = chats
    except ValueError:
        raise ValueError(
            f"conversation {conversation} is bound to several chats: {chats}"
        ) from None
    owner = chat_owner_spawn(runtime_root, chat_id)
    target = spawn_store.get_spawn(runtime_root, str(owner)) if owner is not None else None
    if target is None:
        return None
    hold = begin_continue(
        project_root=project_root,
        runtime_root=runtime_root,
        target=target,
        chat_id=chat_id,
        parent_id=None,
        caller_spawn_id=None,
        dry_run=False,
    )
    return None if hold is None else _Reopen(chat_id, hold)


async def _serve(launch: LaunchPaths, conversation: str, stop: threading.Event) -> None:
    from meridian.lib.launch.process.primary_control_socket import serving_route

    async def deliver(text: str, sender: MessageSender | None) -> DeliveryKind:
        await asyncio.to_thread(inbox.enqueue, conversation, text, sender)
        return DeliveryKind.QUEUED

    async with serving_route(launch, MessageRoute(deliver, _ROUTE_NOTE)):
        await asyncio.to_thread(stop.wait)


__all__ = ["ConversationHolds", "HeldRow"]
