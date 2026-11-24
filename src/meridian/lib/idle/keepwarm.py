"""Keepwarm for harnesses whose sensor takes prompts (``PromptSink``): the stretch's record, the
reply rule, and the driver that times the prompts. Claude's mod keeps its own copy of the record
and the rule in TypeScript (``claude_runtime/meridian-idle/hooks/keepwarm.ts``); change them
together."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from meridian.lib.harness.idle_types import IdleEvent, PromptSink, SidecarContext
from meridian.lib.idle.service import ArmResult, IdleService

if TYPE_CHECKING:
    from meridian.lib.idle.sidecar import SidecarClock

STOP = "stop-waiting"
# Every check before a ping stops this long before the cache would expire.
EXPIRY_MARGIN_MS = 60_000

_QUOTES = (("'", "'"), ('"', '"'), ("\u2018", "\u2019"), ("\u201c", "\u201d"), ("`", "`"))


def reply_stops(reply: str) -> bool:
    """Whether the reply's last non-empty line is ``stop-waiting``, alone or in one pair of
    quotes or backticks (``stop-waiting.`` is not)."""

    lines = reply.strip().splitlines()
    line = lines[-1].strip() if lines else ""
    for opening, closing in _QUOTES:
        if len(line) >= 2 and line.startswith(opening) and line.endswith(closing):
            line = line[len(opening) : len(line) - len(closing)]
            break
    return line == STOP


@dataclass
class Stretch:
    """What the sidecar remembers of one stretch's keepwarm. In memory only: a sidecar that
    restarts has none, which sends no more pings (never an extra one)."""

    stretch: int
    # Pings this stretch may take, the reminder's slot included; 0 when keepwarm is off.
    limit: int
    # The gap core placed between a turn's end and the warning, from the arm that placed it.
    lead_ms: int | None = None
    sent: int = 0
    warned: bool = False
    ended: bool = False

    # A reminder the warning could not send is the stretch's next prompt, before any ping.
    reminder_owed: bool = False

    def next_kind(self) -> Literal["reminder", "ping"] | None:
        """What the stretch sends next, as the mod's ``nextKind``: an owed reminder (until the
        stretch's slots are used up), else a ping while keepwarm is on and slots remain."""

        if self.ended or (self.limit and self.sent >= self.limit):
            return None
        if self.reminder_owed:
            return "reminder"
        return "ping" if self.limit else None

    def due(self) -> bool:
        return self.warned and self.lead_ms is not None and self.next_kind() is not None


class Keepwarm:
    """Times the prompts a ``PromptSink`` gets in a stretch: the state reminder when the warning
    acts, then keepwarm pings. Every prompt goes through ``_send_next``."""

    def __init__(
        self,
        sink: PromptSink,
        *,
        ctx: SidecarContext,
        service: IdleService,
        clock: SidecarClock,
        harness: str,
        busy: Callable[[], Awaitable[bool]],
        report: Callable[[BaseException], None],
    ) -> None:
        self._sink = sink
        self._ctx = ctx
        self._service = service
        self._clock = clock
        self._harness = harness
        self._busy = busy
        self._report = report
        self._stretch: Stretch | None = None
        self._ping_task: asyncio.Task[None] | None = None

    async def on_warn(self, arm: ArmResult, *, remind: bool, due_ms: int) -> None:
        """The warning acted: send the state reminder, else ping 1 when keepwarm is on. The
        reminder takes ping 1's slot with keepwarm on and none with it off."""

        kw = self._stretch if self._stretch and self._stretch.stretch == arm.stretch else None
        if kw is None:
            # A sidecar with no record sends the reminder and no pings.
            assert arm.stretch is not None
            kw = Stretch(arm.stretch, limit=0)
        else:
            kw.warned = True
        kw.reminder_owed = kw.reminder_owed or remind
        warn_ms = await self._warn_ms()
        await self._send_next(kw, cold_at=due_ms + warn_ms - EXPIRY_MARGIN_MS)

    async def on_turn_end(self, event: IdleEvent, result: ArmResult) -> None:
        """Keep the stretch's record from an arm: its start, its lead, its reply; schedule the
        next ping."""

        if result.stretch is None:
            return
        policy = (await asyncio.to_thread(self._service.config_for, self._harness)).policy
        kw = self._stretch
        if kw is None or kw.stretch != result.stretch:
            await self._cancel_ping()
            limit = policy.keepwarm_pings if policy.keepwarm else 0
            kw = self._stretch = Stretch(result.stretch, limit)
        now = self._clock.now_ms()
        if kw.lead_ms is None and result.warn_at is not None:
            kw.lead_ms = result.warn_at - now
        if any(reply_stops(reply) for reply in event.prompt_replies):
            kw.ended = True
        if kw.ended or event.implies_return:
            await self._cancel_ping()
        elif kw.due():
            await self._cancel_ping()
            assert kw.lead_ms is not None
            fire_at = now + kw.lead_ms
            cold_at = fire_at + policy.warn_minutes * 60_000 - EXPIRY_MARGIN_MS
            self._ping_task = asyncio.create_task(self._ping_safely(kw, fire_at, cold_at))

    async def close(self) -> None:
        """A return or teardown: cancel the pending ping and forget the stretch."""

        self._stretch = None
        await self._cancel_ping()

    async def _warn_ms(self) -> int:
        config = await asyncio.to_thread(self._service.config_for, self._harness)
        return config.policy.warn_minutes * 60_000

    async def _cancel_ping(self) -> None:
        task = self._ping_task
        self._ping_task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task

    async def _ping_safely(self, kw: Stretch, fire_at: int, cold_at: int) -> None:
        try:
            await self._ping(kw, fire_at, cold_at)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._report(exc)

    async def _ping(self, kw: Stretch, fire_at: int, cold_at: int) -> None:
        """One later prompt, one lead after a turn that ended in this stretch. A turn running
        then skips it without a slot: that turn's own end schedules the next."""

        await self._clock.sleep_until_ms(fire_at)
        if not self._ctx.tui_alive() or kw.ended:
            return
        if await self._busy():
            return
        # Something else may have closed the stretch (a new process, ``meridian idle return``):
        # only the store says so.
        states = await asyncio.to_thread(
            self._service.status, harness=self._harness, session=self._ctx.harness_session_id
        )
        if not states or not states[0].stretch_open or states[0].stretch != kw.stretch:
            kw.ended = True
            return
        await self._send_next(kw, cold_at=cold_at)

    async def _send_next(self, kw: Stretch, *, cold_at: int) -> None:
        """Send the stretch's next prompt, an owed reminder else a ping, unless the cache is
        cold by ``cold_at``: then the stretch ends, since a prompt would only prefill again."""

        if self._clock.now_ms() >= cold_at:
            kw.ended = True
            return
        kind = kw.next_kind()
        if kind is None:
            return
        on = kw.limit > 0
        # The slot is taken before the send and given back only when the sensor says nothing
        # was queued: a send cancelled mid-way (its inbox write still lands) stays spent, so
        # a stretch can lose a ping but never exceed its limit.
        if on:
            kw.sent += 1
        if kind == "reminder":
            kw.reminder_owed = False
        if not await self._sink.send(kind, stop_offered=on):
            if on:
                kw.sent -= 1
            if kind == "reminder":
                kw.reminder_owed = True
