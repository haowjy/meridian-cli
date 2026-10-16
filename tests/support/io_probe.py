"""Opt-in counters for session reads/folds and actual OS lock holds.

Call install(emit) in sitecustomize before importing Meridian. The hook uses
acquire/release, so lock_file's reentrant scopes are not counted as new holds.
Names for per-chat/native locks and iter_chats also cover the B2 seam.
"""

import time
import traceback
from pathlib import Path


def install(emit):
    from meridian.lib.platform import locking
    from meridian.lib.state import event_store, jsonl

    def caller():
        return [f"{f.name}@{f.filename}:{f.lineno}" for f in traceback.extract_stack(limit=8)[:-2]]

    held = {}
    acquire, release = locking.acquire_file_lock, locking.release_file_lock

    def acquire_probe(path, **kwargs):
        handle = acquire(path, **kwargs)
        held[id(handle)] = (str(path), time.monotonic_ns(), caller())
        return handle

    def release_probe(handle):
        entry = held.pop(id(handle), None)
        try:
            release(handle)
        finally:
            if entry is not None:
                path, started, stack = entry
                emit(
                    dict(
                        kind="lock_hold",
                        path=path,
                        hold_ns=time.monotonic_ns() - started,
                        caller=stack,
                    )
                )

    locking.acquire_file_lock = acquire_probe
    locking.release_file_lock = release_probe
    read = event_store.read_events

    def read_probe(path, parse, **kwargs):
        if Path(path).name == "sessions.jsonl":
            emit(
                dict(
                    kind="read_events",
                    path=str(path),
                    filtered=kwargs.get("raw_filter") is not None,
                    caller=caller(),
                )
            )
        return read(path, parse, **kwargs)

    event_store.read_events = read_probe
    iterate, iterate_file = jsonl.iter_jsonl, jsonl.iter_jsonl_file

    def iter_probe(path, parse, **kwargs):
        if Path(path).name == "sessions.jsonl":
            emit(
                dict(
                    kind="iter_jsonl",
                    path=str(path),
                    filtered=kwargs.get("raw_filter") is not None,
                    offset=kwargs.get("offset", 0),
                    caller=caller(),
                )
            )
        yield from iterate(path, parse, **kwargs)

    def iter_file_probe(handle, parse, *, path, **kwargs):
        if Path(path).name == "sessions.jsonl":
            emit(
                dict(
                    kind="iter_jsonl_file",
                    path=str(path),
                    filtered=kwargs.get("raw_filter") is not None,
                    offset=kwargs.get("offset", 0),
                    caller=caller(),
                )
            )
        yield from iterate_file(handle, parse, path=path, **kwargs)

    jsonl.iter_jsonl = iter_probe
    event_store.iter_jsonl = iter_probe
    jsonl.iter_jsonl_file = iter_file_probe
    from meridian.lib.state import session_store

    session_store.read_events = read_probe
    iterate_chats = getattr(session_store, "iter_chats", None)
    if iterate_chats is not None:

        def chats_probe(*args, **kwargs):
            emit(dict(kind="iter_chats", caller=caller()))
            yield from iterate_chats(*args, **kwargs)

        session_store.iter_chats = chats_probe
