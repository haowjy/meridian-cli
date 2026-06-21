"""Spawn-context multiprocessing race harness for integration tests.

Runs *N* worker processes under ``multiprocessing.get_context("spawn")`` with a
ready barrier so every child arms before any is released, maximizing real
contention instead of hoping processes overlap by accident.

Workers are plain callables ``( *user_args ) -> result``. The harness injects
the barrier; user code should only perform the race-sensitive operation.

Example::

    from tests.support.process_race import run_spawn_race_or_skip

    outcome = run_spawn_race_or_skip(
        reserve_chat_id,
        [(runtime_root,) for _ in range(8)],
    )
    outcome.assert_all_succeeded()
    assert sorted(outcome.results) == [f"c{i}" for i in range(1, 9)]
"""

from __future__ import annotations

import contextlib
import io
import multiprocessing
import time
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

T = TypeVar("T")
_MISSING = object()


class SpawnRaceUnavailableError(RuntimeError):
    """Raised when spawn multiprocessing primitives are unavailable."""


@dataclass(frozen=True)
class WorkerOutcome(Generic[T]):
    """Per-worker result collected after a spawn race."""

    index: int
    result: T | None
    exitcode: int | None
    stderr: str
    error: str | None = None
    has_result: bool = False


@dataclass(frozen=True)
class SpawnRaceOutcome(Generic[T]):
    """Aggregate outcome from :func:`run_spawn_race`."""

    workers: list[WorkerOutcome[T]]

    @property
    def results(self) -> list[T]:
        """Worker return values ordered by worker index."""
        ordered = sorted(self.workers, key=lambda worker: worker.index)
        failures = [
            f"worker {worker.index}: {worker.error} (stderr={worker.stderr!r})"
            for worker in ordered
            if worker.error is not None
        ]
        if failures:
            raise AssertionError("spawn race worker(s) raised:\n" + "\n".join(failures))
        missing = [worker.index for worker in ordered if not worker.has_result]
        if missing:
            raise AssertionError(
                "spawn race missing result(s) from worker(s): " + ", ".join(map(str, missing))
            )
        return [worker.result for worker in ordered]  # type: ignore[misc]

    def assert_all_succeeded(self) -> None:
        """Fail with child stderr when any worker exited non-zero."""
        failures: list[str] = []
        for worker in self.workers:
            if worker.exitcode != 0 or worker.error is not None:
                detail = worker.error or f"exitcode={worker.exitcode!r}"
                failures.append(
                    f"worker {worker.index}: {detail} (stderr={worker.stderr!r})"
                )
        if failures:
            raise AssertionError("spawn race worker(s) failed:\n" + "\n".join(failures))


def _race_entrypoint(
    index: int,
    target: Callable[..., Any],
    user_args: tuple[Any, ...],
    ready_queue: Any,
    release_event: Any,
    result_queue: Any,
) -> None:
    stderr_buffer = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr_buffer):
            ready_queue.put(index)
            release_event.wait()
            outcome = target(*user_args)
            result_queue.put(("result", index, outcome))
    except BaseException as exc:
        traceback.print_exc(file=stderr_buffer)
        result_queue.put(("error", index, repr(exc)))
        raise
    finally:
        result_queue.put(("stderr", index, stderr_buffer.getvalue()))


def run_spawn_race(
    target: Callable[..., T],
    worker_args: Sequence[tuple[Any, ...]],
    *,
    timeout: float = 120.0,
) -> SpawnRaceOutcome[T]:
    """Run *target* in parallel spawned workers released by a ready barrier.

    Parameters
    ----------
    target:
        Callable invoked in each child after the barrier releases. Must be
        importable top-level (spawn-safe).
    worker_args:
        One argument tuple per worker. ``len(worker_args)`` is the worker count.
    timeout:
        Single overall deadline covering arm, execute, join, and result drain.

    Returns
    -------
    SpawnRaceOutcome
        Per-worker results, exit codes, stderr, and captured exceptions.

    Raises
    ------
    SpawnRaceUnavailableError
        When spawn queues/events cannot be created or processes cannot start.
    AssertionError
        On overall timeout or incomplete worker result collection.
    """
    worker_count = len(worker_args)
    if worker_count == 0:
        raise ValueError("worker_args must contain at least one worker")

    try:
        ctx = multiprocessing.get_context("spawn")
        ready_queue = ctx.Queue()
        result_queue = ctx.Queue()
        release_event = ctx.Event()
    except PermissionError as exc:
        raise SpawnRaceUnavailableError(
            f"multiprocessing semaphore unavailable in this environment: {exc}"
        ) from exc

    processes: list[Any] = []
    for index, args in enumerate(worker_args):
        proc = ctx.Process(
            target=_race_entrypoint,
            args=(index, target, args, ready_queue, release_event, result_queue),
        )
        processes.append(proc)

    deadline = time.monotonic() + timeout

    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    try:
        for proc in processes:
            try:
                proc.start()
            except PermissionError as exc:
                raise SpawnRaceUnavailableError(
                    f"multiprocessing semaphore unavailable in this environment: {exc}"
                ) from exc

        armed: set[int] = set()
        while len(armed) < worker_count:
            wait = remaining()
            if wait == 0.0:
                raise AssertionError(
                    f"spawn race timed out after {timeout}s waiting for "
                    f"{worker_count - len(armed)} worker(s) to arm"
                )
            armed.add(ready_queue.get(timeout=wait))

        release_event.set()

        for proc in processes:
            wait = remaining()
            if wait == 0.0:
                if proc.is_alive():
                    proc.terminate()
                    proc.join()
                raise AssertionError(
                    f"spawn race timed out after {timeout}s waiting for workers to exit"
                )
            proc.join(timeout=wait)
            if proc.is_alive():
                proc.terminate()
                proc.join()
                raise AssertionError(
                    f"spawn race timed out after {timeout}s waiting for workers to exit"
                )

        results: dict[int, Any] = {index: _MISSING for index in range(worker_count)}
        errors: dict[int, str] = {}
        stderrs: dict[int, str] = {index: "" for index in range(worker_count)}
        messages_expected = worker_count * 2
        messages_received = 0
        while messages_received < messages_expected:
            wait = remaining()
            if wait == 0.0:
                raise AssertionError(
                    f"spawn race timed out after {timeout}s collecting worker results"
                )
            kind, index, payload = result_queue.get(timeout=wait)
            messages_received += 1
            if kind == "result":
                results[index] = payload
            elif kind == "error":
                errors[index] = payload
            elif kind == "stderr":
                stderrs[index] = payload

        workers = [
            WorkerOutcome(
                index=index,
                result=None if results[index] is _MISSING else results[index],
                exitcode=processes[index].exitcode,
                stderr=stderrs.get(index, ""),
                error=errors.get(index),
                has_result=results[index] is not _MISSING,
            )
            for index in range(worker_count)
        ]
        return SpawnRaceOutcome(workers=workers)
    finally:
        for proc in processes:
            if proc.is_alive():
                proc.terminate()
                proc.join()


def run_spawn_race_or_skip(
    target: Callable[..., T],
    worker_args: Sequence[tuple[Any, ...]],
    *,
    timeout: float = 120.0,
) -> SpawnRaceOutcome[T]:
    """Like :func:`run_spawn_race`, but ``pytest.skip`` when spawn is unavailable."""
    import pytest

    try:
        return run_spawn_race(target, worker_args, timeout=timeout)
    except SpawnRaceUnavailableError as exc:
        pytest.skip(str(exc))
