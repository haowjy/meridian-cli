"""``agy``'s idle sensor through ``run_hold_idle``, a real ``IdleService`` and a real store."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from types import MappingProxyType

import pytest

from meridian.lib.core.types import HarnessId, SpawnId
from meridian.lib.harness.agy.idle import AgyIdleSensor
from meridian.lib.harness.agy.state import busy_path, native_store
from meridian.lib.harness.idle_types import SidecarContext
from meridian.lib.launch.hold_idle import run_hold_idle
from meridian.lib.state.idle_store import IdleState, IdleStore
from meridian.lib.state.paths import LaunchPaths

CONVERSATION = "c1"


class Session:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.proc = root / "proc"
        self.proc.mkdir()
        busy_path(self.proc, CONVERSATION).parent.mkdir()
        self.transcript = (
            native_store() / CONVERSATION / ".system_generated" / "logs" / "transcript_full.jsonl"
        )
        self.transcript.parent.mkdir(parents=True)
        self.steps = 0
        self.store = IdleStore(root=root / "idle")

    def state(self) -> IdleState | None:
        return self.store.read("agy", CONVERSATION)

    async def turn(self, *, typed: bool) -> None:
        """A turn: the mark is created, the transcript grows, the mark goes."""

        busy_path(self.proc, CONVERSATION).touch()
        await asyncio.sleep(0.1)
        step = (
            {
                "source": "USER_EXPLICIT",
                "type": "USER_INPUT",
                "content": "<USER_REQUEST>\nhi\n</USER_REQUEST>",
            }
            if typed
            else {"source": "SYSTEM", "type": "SYSTEM_MESSAGE", "content": "task exited"}
        )
        step |= {"step_index": self.steps, "created_at": "2026-11-23T22:15:50Z"}
        with self.transcript.open("a") as handle:
            handle.write(json.dumps(step) + "\n")
        self.steps += 1
        await asyncio.sleep(0.05)
        busy_path(self.proc, CONVERSATION).unlink()

    async def run(
        self, until: asyncio.Event, *, readopted: bool = False, watch: asyncio.Future[None]
    ) -> None:
        ctx = SidecarContext(
            harness_id=HarnessId.AGY,
            harness_session_id=CONVERSATION,
            env=MappingProxyType(
                {"MERIDIAN_HOME": str(self.root), "MERIDIAN_PROJECT_DIR": str(self.root)}
            ),
            tui_alive=lambda: not until.is_set(),
            launch=LaunchPaths(self.root / "runtime", SpawnId("p1"), None),
        )
        task = asyncio.create_task(
            run_hold_idle(
                lambda stored: AgyIdleSensor(
                    self.proc, CONVERSATION, stored_turn_id=stored, poll_seconds=0.02
                ),
                ctx,
                readopted=readopted,
            )
        )
        await watch
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def _until(check: Callable[[], bool]) -> None:
    for _ in range(300):
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the idle state never reached the expected shape")


@pytest.fixture
def session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Session:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MERIDIAN_HOME", str(tmp_path))
    return Session(tmp_path)


def _drive(session: Session, *, readopted: bool, script: Callable[[], Awaitable[None]]) -> None:
    async def main() -> None:
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        async def play() -> None:
            await script()
            done.set_result(None)

        player = asyncio.create_task(play())
        await session.run(asyncio.Event(), readopted=readopted, watch=done)
        await player

    asyncio.run(main())


def test_typed_turn_opens_a_stretch_and_a_wake_continues_it(session: Session) -> None:
    async def script() -> None:
        await asyncio.sleep(0.1)
        await session.turn(typed=True)
        await _until(lambda: (s := session.state()) is not None and s.last_turn_id == "0")
        opened = session.state()
        assert opened is not None
        await session.turn(typed=False)
        await _until(lambda: (s := session.state()) is not None and s.anchor > opened.anchor)
        woken = session.state()
        assert woken is not None
        assert woken.stretch == opened.stretch
        assert woken.last_turn_id == "0"

    busy_path(session.proc, CONVERSATION).touch()  # a turn is already running at adoption
    _drive(session, readopted=False, script=script)


def test_a_restarted_sensor_does_not_take_a_wake_for_a_return(session: Session) -> None:
    async def first() -> None:
        await asyncio.sleep(0.1)
        await session.turn(typed=True)
        await _until(lambda: (s := session.state()) is not None and s.last_turn_id == "0")

    busy_path(session.proc, CONVERSATION).touch()
    _drive(session, readopted=False, script=first)
    typed = session.state()
    assert typed is not None

    async def second() -> None:
        await asyncio.sleep(0.1)
        busy_path(session.proc, CONVERSATION).touch()
        await session.turn(typed=False)
        await asyncio.sleep(0.3)

    # A new agy process: the stretch is closed, the sidecar's first arm opens the next one,
    # and the wake must continue that one instead of opening a third.
    _drive(session, readopted=False, script=second)
    after = session.state()
    assert after is not None
    assert after.stretch == typed.stretch + 1


def test_a_wake_after_a_same_process_readoption_continues_the_stretch(session: Session) -> None:
    async def first() -> None:
        await asyncio.sleep(0.1)
        await session.turn(typed=True)
        await _until(lambda: (s := session.state()) is not None and s.last_turn_id == "0")

    busy_path(session.proc, CONVERSATION).touch()
    _drive(session, readopted=False, script=first)
    typed = session.state()
    assert typed is not None

    async def second() -> None:
        await asyncio.sleep(0.1)
        await session.turn(typed=False)
        await _until(lambda: (s := session.state()) is not None and s.anchor > typed.anchor)

    _drive(session, readopted=True, script=second)
    after = session.state()
    assert after is not None
    assert after.stretch == typed.stretch
    assert after.last_turn_id == "0"


def test_the_reply_to_a_prompt_meridian_sent_is_reported_with_the_turn_end(
    session: Session,
) -> None:
    import os

    from meridian.lib.harness.agy import inbox
    from meridian.lib.harness.agy.state import watch_lock_path
    from meridian.lib.platform import fcntl

    sensor = AgyIdleSensor(session.proc, CONVERSATION, stored_turn_id=None, poll_seconds=0.02)
    lock = watch_lock_path(session.proc, CONVERSATION)
    lock.parent.mkdir(parents=True, exist_ok=True)

    def write_turn(first: str, reply: str) -> None:
        steps = (
            {"type": "SYSTEM_MESSAGE", "content": first},
            {"type": "PLANNER_RESPONSE", "source": "MODEL", "content": reply},
        )
        with session.transcript.open("a") as handle:
            for step in steps:
                step |= {"step_index": session.steps, "created_at": "2026-11-23T22:15:50Z"}
                handle.write(json.dumps(step) + "\n")
                session.steps += 1

    async def end_turn() -> None:
        busy_path(session.proc, CONVERSATION).unlink()
        await asyncio.sleep(0.2)
        busy_path(session.proc, CONVERSATION).touch()  # the next turn starts
        await asyncio.sleep(0.2)

    async def main() -> list[tuple[str, ...]]:
        replies: list[tuple[str, ...]] = []

        async def collect() -> None:
            async for event in sensor.events():
                replies.append(event.prompt_replies)
                if len(replies) == 2:
                    return

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.1)
        fd = os.open(lock, os.O_RDONLY | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)  # a watcher runs
        try:
            assert await sensor.send("ping", stop_offered=True, expires_at=2**62)
        finally:
            os.close(fd)
        (entry,) = inbox.claim(CONVERSATION, None)
        busy_path(session.proc, CONVERSATION).touch()  # the wake's turn starts
        await asyncio.sleep(0.1)
        write_turn(f"[meridian:{entry.id}] ping", "ok\nstop-waiting")
        await end_turn()
        write_turn("task exited", "later")
        await end_turn()
        await asyncio.wait_for(task, 3)
        return replies

    # The turn that carried the marker reports its reply; the next one reports none.
    assert asyncio.run(main()) == [("ok\nstop-waiting",), ()]


def test_the_sensor_is_busy_from_the_mark_going_until_the_turn_end_is_reported(
    session: Session,
) -> None:
    sensor = AgyIdleSensor(session.proc, CONVERSATION, stored_turn_id=None, poll_seconds=0.5)

    async def main() -> tuple[bool, bool, bool]:
        session.transcript.touch()
        events = sensor.events()
        busy_path(session.proc, CONVERSATION).touch()
        assert await sensor.busy()
        first = asyncio.ensure_future(anext(events))
        await asyncio.sleep(0.05)
        busy_path(session.proc, CONVERSATION).unlink()
        gone = await sensor.busy()  # before the poll reports the end: still busy
        await first
        reported = await sensor.busy()  # yielded, the consumer has not asked for more yet
        nxt = asyncio.ensure_future(anext(events))
        await asyncio.sleep(0.7)
        after = await sensor.busy()
        nxt.cancel()
        return gone, reported, after

    assert asyncio.run(main()) == (True, True, False)
