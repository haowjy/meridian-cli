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
