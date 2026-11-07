"""OpenCode relocate: move sessions through OpenCode's own ``POST /api/session/<id>/move``.

OpenCode refuses to resume a session while its recorded directory is gone, and keeps
that directory in its DB. Meridian never writes the DB: one short-lived
``opencode serve`` per DB, pinned with ``OPENCODE_DB``, does the move. The serve runs
in its own process group and the group is killed on every exit path that Python can
see (SIGTERM and SIGHUP included; SIGKILL cannot be). ``/move`` answers 204 at once and
the row changes about a second later, so each move is confirmed by reading the row back
before the serve stops.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from collections.abc import Generator, Sequence
from contextlib import closing
from pathlib import Path
from types import FrameType
from typing import IO

from meridian.lib.core.native_identity import NativeKeyFields
from meridian.lib.harness.adapter import NativeRelocation, NativeRelocationResult
from meridian.lib.harness.connections.opencode_http import find_free_port
from meridian.lib.harness.opencode_transcript import connect_readonly
from meridian.lib.launch.constants import BASE_COMMAND_OPENCODE_STREAMING

_SERVE_READY_SECONDS = 30.0
_MOVE_APPLIED_SECONDS = 15.0
_REQUEST_SECONDS = 10.0
_PASSWORD_RE = re.compile(r"server password (\S+)")


def opencode_native_cwd(identity: NativeKeyFields) -> Path | None:
    if identity.native_store is None or identity.session_id is None:
        return None
    return _read_directory(Path(identity.native_store), identity.session_id)


def plan_opencode_relocate(
    identity: NativeKeyFields, *, cwd_from: Path, cwd_to: Path
) -> NativeRelocation | None:
    if not identity.native_store:
        return None
    db = Path(identity.native_store)
    return NativeRelocation(
        identity,
        cwd_from,
        cwd_to,
        f"OpenCode session directory {cwd_from} -> {cwd_to} (POST /api/session/<id>/move in {db})",
        source=db,
    )


def apply_opencode_relocations(
    plans: Sequence[NativeRelocation],
) -> tuple[NativeRelocationResult, ...]:
    refusals: dict[int, str | None] = {}
    by_db: dict[Path, list[tuple[int, NativeRelocation]]] = defaultdict(list)
    for index, plan in enumerate(plans):
        assert plan.source is not None  # a plan without a refusal names the DB
        by_db[plan.source].append((index, plan))
    for db, entries in by_db.items():
        moves: dict[str, Path] = {}
        for index, plan in entries:
            session_id = plan.identity.session_id or ""
            current = _read_directory(db, session_id)
            if current is None:
                refusals[index] = f"{db}: session {session_id} has no directory record"
            elif current == plan.cwd_to:
                refusals[index] = None  # already done
            elif current == plan.cwd_from:
                moves[session_id] = plan.cwd_to
            else:
                refusals[index] = f"{db}: session {session_id} changed since the plan"
        failures = _move_sessions(db, moves) if moves else {}
        for index, plan in entries:
            session_id = plan.identity.session_id or ""
            if index not in refusals:
                refusals[index] = failures.get(session_id)
    return tuple(NativeRelocationResult(plan, refusals[i]) for i, plan in enumerate(plans))


class _Termination:
    """SIGTERM and SIGHUP as an exit, so the ``finally`` that reaps the serve runs.

    The serve has its own session, so a closed terminal never reaches it; Python's
    default handlers exit without unwinding. Inside ``held()`` a signal waits until the
    block ends: no exit may cut the serve's start or its stop short. A signal the
    process inherited as ignored (``nohup``) stays ignored. Only the main thread can
    install handlers.
    """

    def __init__(self) -> None:
        self._held = False
        self._pending: int | None = None

    def _handle(self, signum: int, _frame: FrameType | None) -> None:
        if self._held:
            self._pending = signum
        else:
            raise SystemExit(128 + signum)

    @contextlib.contextmanager
    def installed(self) -> Generator[None]:
        if threading.current_thread() is not threading.main_thread():
            yield
            return
        previous = {
            sig: signal.signal(sig, self._handle)
            for sig in (signal.SIGTERM, signal.SIGHUP)
            if signal.getsignal(sig) is not signal.SIG_IGN
        }
        try:
            yield
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    @contextlib.contextmanager
    def held(self) -> Generator[None]:
        self._held = True
        try:
            yield
        finally:
            self._held = False
            if self._pending is not None:
                raise SystemExit(128 + self._pending)


def _move_sessions(db: Path, moves: dict[str, Path]) -> dict[str, str]:
    termination = _Termination()
    with tempfile.TemporaryFile() as out, termination.installed():
        process: subprocess.Popen[bytes] | None = None
        try:
            with termination.held():  # no gap between the fork and owning the group
                process = _start_serve(db, find_free_port(), out)
            base_url, auth = _await_ready(process, out)
            failures: dict[str, str] = {}
            for session_id, target in moves.items():
                reason = _post_move(base_url, auth, session_id, target)
                if reason is None:
                    reason = _await_applied(db, session_id, target)
                if reason is not None:
                    failures[session_id] = reason
            return failures
        except RuntimeError as exc:
            return dict.fromkeys(moves, f"{db}: {exc}")
        finally:
            if process is not None:
                with termination.held():
                    _stop_group(process)


def _start_serve(db: Path, port: int, out: IO[bytes]) -> subprocess.Popen[bytes]:
    command = [*BASE_COMMAND_OPENCODE_STREAMING, "--hostname", "127.0.0.1", "--port", str(port)]
    try:
        return subprocess.Popen(
            command,
            env={**os.environ, "OPENCODE_DB": str(db)},
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        raise RuntimeError(f"cannot start `opencode serve`: {exc}") from exc


def _await_ready(process: subprocess.Popen[bytes], out: IO[bytes]) -> tuple[str, str]:
    """The serve prints its URL and password on stdout; wait for both."""
    deadline = time.monotonic() + _SERVE_READY_SECONDS
    while time.monotonic() < deadline:
        out.seek(0)
        text = out.read().decode("utf-8", errors="replace")
        listening = re.search(r"listening on (http://\S+)", text)
        password = _PASSWORD_RE.search(text)
        if listening and password:
            token = base64.b64encode(f"opencode:{password.group(1)}".encode()).decode()
            return listening.group(1), f"Basic {token}"
        if process.poll() is not None:
            raise RuntimeError(f"`opencode serve` exited ({process.returncode}): {text.strip()}")
        time.sleep(0.1)
    raise RuntimeError(f"`opencode serve` was not ready after {_SERVE_READY_SECONDS:.0f}s")


def _post_move(base_url: str, auth: str, session_id: str, target: Path) -> str | None:
    request = urllib.request.Request(
        f"{base_url}/api/session/{session_id}/move",
        data=json.dumps({"directory": str(target)}).encode(),
        headers={"Authorization": auth, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_REQUEST_SECONDS):
            return None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        hint = "; an OpenCode without the move endpoint answers 404" if exc.code == 404 else ""
        return f"POST /api/session/{session_id}/move answered {exc.code}: {body}{hint}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return f"POST /api/session/{session_id}/move failed: {exc}"


def _await_applied(db: Path, session_id: str, target: Path) -> str | None:
    deadline = time.monotonic() + _MOVE_APPLIED_SECONDS
    while time.monotonic() < deadline:
        if _read_directory(db, session_id) == target:
            return None
        time.sleep(0.2)
    return (
        f"session {session_id} still not at {target} after {_MOVE_APPLIED_SECONDS:.0f}s "
        "(is it running a turn?)"
    )


def _stop_group(process: subprocess.Popen[bytes]) -> None:
    for sig, grace in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 5.0)):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, sig)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=grace)
            return


def _read_directory(db: Path, session_id: str) -> Path | None:
    if not db.is_file():
        return None
    try:
        with closing(connect_readonly(db)) as connection:
            row = connection.execute(
                "SELECT directory FROM session_v2 WHERE id = ?", (session_id,)
            ).fetchone()
    except sqlite3.Error:
        return None  # no session_v2 table (V1 DB) or an unreadable DB: no record
    return Path(row[0]) if row and isinstance(row[0], str) and row[0] else None
