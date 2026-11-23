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
from contextlib import nullcontext
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
# A conversation whose hold failed is retried by the poll no sooner than this; a tool call
# for it retries at once.
_RETRY_SECONDS = 10.0
_ROUTE_NOTE = (
    "agy has no push channel: the session reads the message in its next turn. An idle session "
    "wakes for it when its Meridian watcher runs; otherwise it waits until the person types."
)


class HoldError(RuntimeError):
    """A conversation has no row to run as; the text says why and is shown to the model."""


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
    error: str | None = None
    thread: threading.Thread | None = None


class ConversationHolds:
    def __init__(self, proc_dir: Path) -> None:
        self._proc_dir = proc_dir
        self._holds: dict[str, _Hold] = {}
        self._failures: dict[str, tuple[str, float]] = {}  # conversation -> (error, when)
        self._lock = threading.Lock()

    def sync(self) -> None:
        """Adopt every primary conversation of this ``agy`` process that has no hold yet."""

        for path in sorted((self._proc_dir / "requests").glob("*.json")):
            with self._lock:
                failure = self._failures.get(path.stem)
            if failure is None or time.monotonic() - failure[1] >= _RETRY_SECONDS:
                self._adopt(path.stem)

    def row_for(self, conversation: str) -> HeldRow:
        """The row held for ``conversation``, adopting it first when its request just arrived.

        Raises ``HoldError`` when there is none, and a failed hold is dropped, so the next
        call or poll adopts again.
        """

        hold = self._adopt(conversation)
        if hold is None:
            raise HoldError(
                f"Meridian has no session for Antigravity conversation {conversation}: its "
                "SessionStart hook did not register it, so nothing ran. Check that "
                "`meridian mars sync` installed the plugin's hooks, then send another prompt."
            )
        hold.ready.wait(_ADOPT_WAIT_SECONDS)
        if hold.row is None:
            raise HoldError(
                "Meridian could not hold this Antigravity conversation as a session, so "
                f"nothing ran: {hold.error or 'timed out'}. If another agy process has the "
                "conversation open, close it; the next call retries."
            )
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
        except Exception as exc:
            # Whenever it failed: a hold that stays registered keeps handing out a row
            # nobody serves. Dropped, the next call or poll adopts the conversation again.
            self._drop(hold, str(exc) or type(exc).__name__)
        finally:
            hold.ready.set()

    def _drop(self, hold: _Hold, error: str) -> None:
        hold.error = error
        with self._lock:
            if self._holds.get(hold.conversation) is hold:
                del self._holds[hold.conversation]
            previous = self._failures.get(hold.conversation)
            self._failures[hold.conversation] = (error, time.monotonic())
        if previous is None or previous[0] != error:
            logger.warning("agy conversation %s was not held: %s", hold.conversation, error)


def _hold_row(hold: _Hold, request: dict[str, Any], proc_dir: Path) -> None:
    # Imported here: the hook shares this package and must not load the launch stack.
    from meridian.lib.bootstrap.services import build_spawn_application_service_from_roots
    from meridian.lib.config.project_root import resolve_project_root_resolution
    from meridian.lib.core.execution_policy import ResolvedExecutionPolicy
    from meridian.lib.core.launch_policy_snapshot import LaunchPolicySnapshot
    from meridian.lib.core.types import SpawnId
    from meridian.lib.launch.primary_hold import PrimaryRow, primary_hold
    from meridian.lib.launch.request import SessionRequest
    from meridian.lib.launch.session_links import republish_chat_links
    from meridian.lib.launch.types import PrimarySessionMetadata
    from meridian.lib.ops.runtime import resolve_runtime_root
    from meridian.lib.state import spawn_store

    conversation = hold.conversation
    agent = str(request.get("agent") or "")
    model = str(request.get("model") or "")
    # The command the person ran named this agent and approval; the snapshot is what a
    # continue replays, so ``meridian agy --continue`` restores them.
    policy = LaunchPolicySnapshot(
        model=model,
        harness="agy",
        agent=agent or None,
        # The plugin records no rules: a continue asks Mars for the agent's current ones.
        tool_rules_recorded=False,
        execution_policy=ResolvedExecutionPolicy(approval=request.get("approval") or None),
    )
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
            model=model,
            agent=agent,
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
    # The reopened hold's launch slot is released here, whatever raises before or inside
    # primary_hold.
    with (
        resumed.hold.slot if resumed else nullcontext(),
        primary_hold(
            row, spawn_service=service, continue_hold=resumed.hold if resumed else None
        ) as primary,
    ):
        launch = primary.start(work_id=None)
        primary.bind_observed_identity(
            conversation, harness="agy", policy=policy, observed_model=model or None
        )
        service.lifecycle.bootstrap_from_disk(str(launch.spawn_id))
        held = HeldRow(str(launch.spawn_id), primary.chat_id, project_root, runtime_root, cwd)
        # A launched primary gets its links from launch preparation; this one has none, and
        # its hook names them to the model.
        republish_chat_links(project_root, runtime_root, held.chat_id, spawn_id=held.spawn_id)
        write_private_json(
            row_path(proc_dir, conversation),
            {
                "spawn_id": held.spawn_id,
                "chat_id": held.chat_id,
                "project_root": project_root.as_posix(),
                "runtime_root": runtime_root.as_posix(),
            },
        )
        hold.row = held
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
    try:
        hold = begin_continue(
            project_root=project_root,
            runtime_root=runtime_root,
            target=target,
            chat_id=chat_id,
            parent_id=None,
            caller_spawn_id=None,
            dry_run=False,
        )
    except ValueError as exc:
        # Its text ends in CLI advice ("; use spawn message ... or --fork ..."), which the
        # agy model cannot act on: keep the cause.
        raise ValueError(str(exc).partition("; ")[0]) from exc
    return None if hold is None else _Reopen(chat_id, hold)


async def _serve(launch: LaunchPaths, conversation: str, stop: threading.Event) -> None:
    from meridian.lib.launch.process.primary_control_socket import serving_route

    async def deliver(text: str, sender: MessageSender | None) -> DeliveryKind:
        await asyncio.to_thread(inbox.enqueue, conversation, text, sender)
        return DeliveryKind.QUEUED

    async with serving_route(launch, MessageRoute(deliver, _ROUTE_NOTE)):
        await asyncio.to_thread(stop.wait)


__all__ = ["ConversationHolds", "HeldRow", "HoldError"]
