"""Shared runner helper functions used by subprocess and streaming paths."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import cast

import structlog

from meridian.lib.config.settings import MeridianConfig
from meridian.lib.core.domain import Spawn
from meridian.lib.core.types import SpawnId
from meridian.lib.harness.adapter import StreamEvent
from meridian.lib.launch.constants import OUTPUT_FILENAME, STDERR_FILENAME
from meridian.lib.platform.terminate import terminate_tree as _terminate_tree
from meridian.lib.safety.budget import BudgetBreach
from meridian.lib.safety.guardrails import GuardrailFailure
from meridian.lib.state import spawn_store
from meridian.lib.state.artifact_store import ArtifactStore, make_artifact_key
from meridian.lib.state.atomic import atomic_write_bytes

logger = structlog.get_logger(__name__)
DEFAULT_KILL_GRACE_SECONDS = MeridianConfig().kill_grace_minutes * 60.0
DEFAULT_STREAM_READ_CHUNK_SIZE = 64 * 1024


class SpawnTimeoutError(TimeoutError):
    """Raised when a harness process exceeds the configured timeout."""

    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds
        super().__init__(f"Spawn exceeded timeout after {timeout_seconds:.3f}s")


def spawn_kind(runtime_root: Path, spawn_id: SpawnId) -> str:
    row = spawn_store.get_spawn(runtime_root, spawn_id)
    if row is None:
        return "child"
    normalized = row.kind.strip().lower()
    if normalized in {"primary", "child"}:
        return normalized
    return "child"


def append_budget_exceeded_event(*, run: Spawn, breach: BudgetBreach) -> None:
    logger.warning(
        "Spawn budget exceeded.",
        spawn_id=str(run.spawn_id),
        scope=breach.scope,
        observed_usd=breach.observed_usd,
        limit_usd=breach.limit_usd,
    )


def guardrail_failure_text(failures: tuple[GuardrailFailure, ...]) -> str:
    lines = ["Guardrail validation failed:"]
    for failure in failures:
        lines.append(
            f"- {failure.script} (exit {failure.exit_code})"
            + (f": {failure.stderr}" if failure.stderr else "")
        )
    return "\n".join(lines)


def append_text_to_stderr_artifact(
    *,
    artifacts: ArtifactStore,
    spawn_id: SpawnId,
    text: str,
) -> None:
    key = make_artifact_key(spawn_id, STDERR_FILENAME)
    existing = artifacts.get(key).decode("utf-8", errors="ignore") if artifacts.exists(key) else ""
    prefix = "\n" if existing and not existing.endswith("\n") else ""
    combined = f"{existing}{prefix}{text}\n"
    artifacts.put(key, combined.encode("utf-8"))


def artifact_is_zero_bytes(
    *,
    artifacts: ArtifactStore,
    spawn_id: SpawnId,
    filename: str,
) -> bool:
    key = make_artifact_key(spawn_id, filename)
    if not artifacts.exists(key):
        return True
    return len(artifacts.get(key)) == 0


def write_structured_failure_artifact(
    *,
    artifacts: ArtifactStore,
    spawn_id: SpawnId,
    output_log_path: Path,
    exit_code: int,
    failure_reason: str | None,
    timed_out: bool,
) -> None:
    payload = {
        "error_code": "harness_empty_output",
        "failure_reason": failure_reason or "empty_output",
        "exit_code": exit_code,
        "timed_out": timed_out,
    }
    encoded = f"{json.dumps(payload, sort_keys=True)}\n".encode()
    artifacts.put(make_artifact_key(spawn_id, OUTPUT_FILENAME), encoded)
    atomic_write_bytes(output_log_path, encoded)


def _extract_tokens_payload(raw_line: bytes) -> bytes | None:
    try:
        payload_obj = json.loads(raw_line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    if not isinstance(payload_obj, dict):
        return None

    payload = cast("dict[str, object]", payload_obj)
    tokens = payload.get("tokens")
    if not isinstance(tokens, dict):
        return None
    return json.dumps(tokens, sort_keys=True).encode("utf-8")


def extract_latest_tokens_payload(output_bytes: bytes) -> bytes | None:
    latest_payload: bytes | None = None
    for raw_line in output_bytes.splitlines():
        parsed = _extract_tokens_payload(raw_line)
        if parsed is not None:
            latest_payload = parsed
    return latest_payload


async def _iter_stream_lines(
    reader: asyncio.StreamReader,
    *,
    chunk_size: int = DEFAULT_STREAM_READ_CHUNK_SIZE,
) -> AsyncIterator[bytes]:
    pending = bytearray()
    while True:
        chunk = await reader.read(chunk_size)
        if not chunk:
            break
        pending.extend(chunk)
        while True:
            newline_index = pending.find(b"\n")
            if newline_index < 0:
                break
            line = bytes(pending[: newline_index + 1])
            del pending[: newline_index + 1]
            yield line
    if pending:
        yield bytes(pending)


async def capture_stdout_stream(
    reader: asyncio.StreamReader,
    output_file: Path,
    *,
    line_observer: Callable[[bytes], None] | None,
    parse_stream_event: Callable[[str], StreamEvent | None] | None = None,
    event_observer: Callable[[StreamEvent], None] | None = None,
    stream_to_terminal: bool = False,
) -> bytes | None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    last_tokens_payload: bytes | None = None

    with output_file.open("wb") as handle:
        async for raw_line in _iter_stream_lines(reader):
            if line_observer is not None:
                line_observer(raw_line)

            line_text = raw_line.decode("utf-8", errors="replace")
            if parse_stream_event is not None and event_observer is not None:
                try:
                    parsed_event = parse_stream_event(line_text)
                except Exception:
                    logger.warning("Failed to parse harness stream event.", exc_info=True)
                else:
                    if parsed_event is not None:
                        try:
                            event_observer(parsed_event)
                        except Exception:
                            logger.warning("Stream event observer failed.", exc_info=True)

            if stream_to_terminal:
                sys.stderr.write(line_text)
                sys.stderr.flush()

            parsed_tokens = _extract_tokens_payload(raw_line)
            if parsed_tokens is not None:
                last_tokens_payload = parsed_tokens

            handle.write(raw_line)
            handle.flush()

    return last_tokens_payload


async def capture_stderr_stream(
    reader: asyncio.StreamReader,
    stderr_file: Path,
    *,
    stream_to_terminal: bool = False,
    chunk_size: int = DEFAULT_STREAM_READ_CHUNK_SIZE,
) -> bytes:
    stderr_file.parent.mkdir(parents=True, exist_ok=True)
    buffer = bytearray()

    with stderr_file.open("wb") as handle:
        while True:
            chunk = await reader.read(chunk_size)
            if not chunk:
                break
            handle.write(chunk)
            handle.flush()
            buffer.extend(chunk)
            if stream_to_terminal:
                sys.stderr.write(chunk.decode("utf-8", errors="replace"))
                sys.stderr.flush()

    return bytes(buffer)


async def wait_for_process_returncode(
    process: asyncio.subprocess.Process,
    *,
    timeout_seconds: float | None = None,
    poll_interval_seconds: float = 0.05,
) -> int:
    """Wait until the subprocess exit status is available."""

    if timeout_seconds is not None and timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be > 0 when provided.")

    deadline = (
        None if timeout_seconds is None else asyncio.get_running_loop().time() + timeout_seconds
    )
    while process.returncode is None:
        if deadline is not None and asyncio.get_running_loop().time() >= deadline:
            assert timeout_seconds is not None
            raise SpawnTimeoutError(timeout_seconds)
        await asyncio.sleep(poll_interval_seconds)
    return process.returncode


async def terminate_process(
    process: asyncio.subprocess.Process,
    *,
    grace_seconds: float = DEFAULT_KILL_GRACE_SECONDS,
) -> None:
    """Gracefully terminate a process and force-kill if it does not exit."""
    await _terminate_tree(process, grace_secs=grace_seconds)


async def wait_for_process_exit(
    process: asyncio.subprocess.Process,
    *,
    timeout_seconds: float | None,
    kill_grace_seconds: float = DEFAULT_KILL_GRACE_SECONDS,
) -> int:
    """Wait for process completion with timeout-triggered termination."""

    if timeout_seconds is None:
        return await wait_for_process_returncode(process)

    try:
        return await wait_for_process_returncode(process, timeout_seconds=timeout_seconds)
    except SpawnTimeoutError as exc:
        await terminate_process(process, grace_seconds=kill_grace_seconds)
        raise SpawnTimeoutError(timeout_seconds) from exc


__all__ = [
    "DEFAULT_KILL_GRACE_SECONDS",
    "DEFAULT_STREAM_READ_CHUNK_SIZE",
    "SpawnTimeoutError",
    "append_budget_exceeded_event",
    "append_text_to_stderr_artifact",
    "artifact_is_zero_bytes",
    "capture_stderr_stream",
    "capture_stdout_stream",
    "extract_latest_tokens_payload",
    "guardrail_failure_text",
    "spawn_kind",
    "terminate_process",
    "wait_for_process_exit",
    "wait_for_process_returncode",
    "write_structured_failure_artifact",
]
