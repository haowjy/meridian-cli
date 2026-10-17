"""Crash-safe authoritative idle stretch state."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from meridian.lib.harness.idle_types import BaselineSource, CompactEval
from meridian.lib.platform.locking import lock_file
from meridian.lib.state.json_doc import mutate_model, read_model
from meridian.lib.state.user_paths import get_user_home

Stage = Literal["push", "warn", "compact"]
CompactResultValue = Literal["ok", "failed", "vetoed"]
_RETENTION_MS = 7 * 24 * 60 * 60 * 1000
_GC_INTERVAL_MS = 60 * 60 * 1000
T = TypeVar("T")


class IdleSchedule(BaseModel):
    """Persisted absolute stage deadlines."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    push_at: int | None = None
    warn_at: int | None = None
    compact_at: int | None = None


def _empty_done() -> dict[Stage, str]:
    return {}


class ContextBaseline(BaseModel):
    """Trustworthy context observations and the retained growth baseline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    context_tokens: int | None = Field(default=None, ge=0)
    baseline_tokens: int | None = Field(default=None, ge=0)
    baseline_source: BaselineSource = "observed"

    def observe(self, tokens: int | None) -> ContextBaseline:
        if tokens is None or tokens <= 0:
            return self
        baseline, source = self.baseline_tokens, self.baseline_source
        if baseline is None:
            baseline = tokens
        elif self.context_tokens is not None and tokens < self.context_tokens:
            baseline, source = tokens, "observed"
        return ContextBaseline(
            context_tokens=tokens, baseline_tokens=baseline, baseline_source=source
        )

    def after_compaction(self) -> ContextBaseline:
        return self.model_copy(update={"baseline_tokens": None, "baseline_source": "compacted"})


class IdleState(BaseModel):
    """Versioned state for one harness-native session."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    v: Literal[1] = 1
    harness: str
    session: str
    stretch: int = Field(ge=1)
    stretch_open: bool
    last_turn_id: str | None = None
    last_input_count: int | None = Field(default=None, ge=0)
    last_user_text: str | None = None
    last_assistant_text: str | None = None
    anchor: int = Field(ge=1)
    idle_since_ms: int | None
    returned_at_ms: int | None = None
    ttl_seconds: int | None = Field(default=None, gt=0)
    ttl_estimated: bool = True
    context: ContextBaseline = ContextBaseline()
    compact_eval: CompactEval | None = None
    schedule: IdleSchedule
    # Why this anchor scheduled no push: work still running will wake the session.
    push_held: str | None = None
    done: dict[Stage, str] = Field(default_factory=_empty_done)
    compact_window_until_ms: int | None = None
    expect_compaction_turn: bool = False
    updated_at_ms: int

    @field_validator("done")
    @classmethod
    def _validate_done(cls, value: dict[Stage, str]) -> dict[Stage, str]:
        allowed = {
            "push": {"sent", "failed"},
            "warn": {"sent", "failed"},
            "compact": {"claimed", "ok", "failed", "vetoed"},
        }
        for stage, outcome in value.items():
            if outcome not in allowed[stage] and not outcome.startswith("skipped:"):
                raise ValueError(f"Invalid {stage} outcome: {outcome!r}")
        return value


class IdleStoreReader(Protocol):
    """Read/mutate seam used by idle policy and in-memory tests."""

    def read(self, harness: str, session: str) -> IdleState | None: ...

    def mutate(
        self,
        harness: str,
        session: str,
        mutation: Callable[[IdleState | None], tuple[IdleState | None, T]],
    ) -> T: ...

    def list_states(self) -> tuple[IdleState, ...]: ...


def _default_now_ms() -> int:
    return int(time.time() * 1000)


def _validate_component(value: str, *, label: str) -> str:
    normalized = value.strip()
    if not normalized or normalized in {".", ".."} or any(c in normalized for c in "/\\\0"):
        raise ValueError(f"Invalid idle {label}: {value!r}")
    return normalized


class IdleStore:
    """One atomically replaced JSON file per harness-native session."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        now_ms: Callable[[], int] = _default_now_ms,
    ) -> None:
        self.root = root if root is not None else get_user_home() / "idle"
        self._now_ms = now_ms

    def path_for(self, harness: str, session: str) -> Path:
        safe_harness = _validate_component(harness, label="harness")
        safe_session = _validate_component(session, label="session")
        return self.root / f"{safe_harness}-{safe_session}.json"

    def _lock_path(self, path: Path) -> Path:
        return self.root / ".locks" / f"{path.name}.lock"

    @property
    def _gc_marker(self) -> Path:
        return self.root / ".gc"

    def read(self, harness: str, session: str) -> IdleState | None:
        return self._read_path(self.path_for(harness, session))

    @staticmethod
    def _read_path(path: Path) -> IdleState | None:
        return read_model(path, IdleState)

    def mutate(
        self,
        harness: str,
        session: str,
        mutation: Callable[[IdleState | None], tuple[IdleState | None, T]],
    ) -> T:
        path = self.path_for(harness, session)
        def stamp(current: IdleState | None) -> tuple[IdleState | None, T]:
            next_state, result = mutation(current)
            if next_state is not None:
                next_state = next_state.model_copy(update={"updated_at_ms": self._now_ms()})
            return next_state, result

        next_state, result = mutate_model(
            path,
            IdleState,
            stamp,
            lock=lock_file(self._lock_path(path)),
            indent=2,
        )
        if next_state is not None:
            self.gc()
        return result

    def write(self, state: IdleState) -> IdleState:
        """Replace one state under its file lock and return the stamped value."""

        def replace(_current: IdleState | None) -> tuple[IdleState, None]:
            return state, None

        self.mutate(state.harness, state.session, replace)
        written = self.read(state.harness, state.session)
        if written is None:  # pragma: no cover - atomic replacement made this unreachable
            raise OSError("Idle state disappeared after atomic write")
        return written

    def list_states(self) -> tuple[IdleState, ...]:
        if not self.root.is_dir():
            return ()
        states = [self._read_path(path) for path in self.root.glob("*.json")]
        return tuple(state for state in states if state is not None)

    def gc(self) -> None:
        """Delete state older than seven days, rechecking under its stable lock."""

        if not self.root.is_dir():
            return
        now_ms = self._now_ms()
        with lock_file(self._lock_path(self._gc_marker)):
            try:
                last_gc_ms = int(self._gc_marker.stat().st_mtime * 1000)
            except OSError:
                last_gc_ms = None
            if last_gc_ms is not None and now_ms - last_gc_ms < _GC_INTERVAL_MS:
                return
            self._gc_marker.touch()
            marker_ns = now_ms * 1_000_000
            self._gc_marker.chmod(0o600)
            os.utime(self._gc_marker, ns=(marker_ns, marker_ns))

        cutoff = now_ms - _RETENTION_MS
        for path in self.root.glob("*.json"):
            lock_path = self._lock_path(path)
            with lock_file(lock_path):
                state = self._read_path(path)
                if state is not None:
                    stale = state.updated_at_ms < cutoff
                else:
                    try:
                        stale = int(path.stat().st_mtime * 1000) < cutoff
                    except OSError:
                        stale = False
                if stale:
                    with suppress(FileNotFoundError):
                        path.unlink()


__all__ = [
    "CompactResultValue",
    "ContextBaseline",
    "IdleSchedule",
    "IdleState",
    "IdleStore",
    "IdleStoreReader",
    "Stage",
]
