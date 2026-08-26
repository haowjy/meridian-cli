"""Certified append boundary for the authoritative session event journal."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from meridian.lib.platform.locking import lock_file
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.event_store import append_event
from meridian.lib.state.paths import RuntimePaths

if TYPE_CHECKING:
    from pydantic import BaseModel


@dataclass(frozen=True)
class JournalSourceState:
    dev: int
    ino: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class AppendCertificate:
    epoch: str
    source: JournalSourceState


def source_state(path: Path) -> JournalSourceState:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return JournalSourceState(0, 0, 0, 0, 0)
    return JournalSourceState(
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_ctime_ns,
    )


def _read_certificate(paths: RuntimePaths) -> AppendCertificate | None:
    try:
        payload = json.loads(paths.sessions_append_state.read_text(encoding="utf-8"))
        source = payload["source"]
        epoch = payload["epoch"]
        if not isinstance(epoch, str):
            return None
        certificate = AppendCertificate(
            epoch=epoch,
            source=JournalSourceState(
                dev=source["dev"],
                ino=source["ino"],
                size=source["size"],
                mtime_ns=source["mtime_ns"],
                ctime_ns=source["ctime_ns"],
            ),
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not certificate.epoch or not all(
        isinstance(value, int) for value in asdict(certificate.source).values()
    ):
        return None
    return certificate


def certified_epoch(paths: RuntimePaths, state: JournalSourceState) -> str | None:
    certificate = _read_certificate(paths)
    if certificate is None or certificate.source != state:
        return None
    return certificate.epoch


def append_session_event(
    paths: RuntimePaths,
    event: BaseModel,
    *,
    exclude_none: bool = False,
    append_event_fn: Callable[..., None] = append_event,
) -> None:
    """Append one event and certify that the preceding source was unchanged."""

    with lock_file(paths.sessions_flock):
        before = source_state(paths.sessions_jsonl)
        certificate = _read_certificate(paths)
        epoch = (
            certificate.epoch
            if certificate is not None and certificate.source == before
            else uuid.uuid4().hex
        )
        append_event_fn(
            paths.sessions_jsonl,
            paths.sessions_flock,
            event,
            exclude_none=exclude_none,
        )
        updated = AppendCertificate(epoch, source_state(paths.sessions_jsonl))
        # The event is authoritative and already durable. A stale/missing
        # certificate makes the index rebuild rather than trusting a suffix.
        with suppress(OSError):
            atomic_write_text(
                paths.sessions_append_state,
                json.dumps(asdict(updated), separators=(",", ":"), sort_keys=True) + "\n",
            )


__all__ = [
    "JournalSourceState",
    "append_session_event",
    "certified_epoch",
    "source_state",
]
