"""`meridian sync run|now|status`: the foreground daemon and what it says."""

from __future__ import annotations

import signal
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict

from meridian.lib.autosync import state as autosync
from meridian.lib.config.project_root import resolve_project_root_resolution
from meridian.lib.core.formatting import kv_block, relative_time
from meridian.lib.core.util import FormatContext
from meridian.lib.platform.python_module import installed_executable
from meridian.lib.state.user_paths import get_user_home

NOW_TIMEOUT_S = 60.0
_POLL_S = 0.25
# The daemon exits with this when its executable was replaced and wants to be started again.
_RESTART_CODE = 75


class RepairOutput(BaseModel):
    """A stray branch's commits the daemon moved onto the default branch."""

    model_config = ConfigDict(frozen=True)

    from_branch: str
    commits: int
    at: str


class SkippedRecordOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    file: str
    v: int | None = None
    error: str


class SyncContextOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    clone_key: str
    clone_path: str
    state: str
    outcome: str | None = None
    reason: str | None = None
    error: str | None = None
    fix: str | None = None
    note: str | None = None
    default_branch: str | None = None
    current_branch: str | None = None
    ahead: int | None = None
    behind: int | None = None
    last_pass_at: str | None = None
    last_success_at: str | None = None
    stopped_since: str | None = None
    step: str | None = None
    step_started_at: str | None = None
    watch: str | None = None
    last_repair: RepairOutput | None = None

    def line(self) -> str:
        """`kb: synced 2m ago` / `work: STOPPED since …, off_branch`, then the running
        step, the branch, and the fix, one per indented line."""

        if self.state == "synced" and self.last_success_at:
            head = f"synced {relative_time(self.last_success_at)}"
        elif self.state == "stopped":
            since = f" since {relative_time(self.stopped_since)}" if self.stopped_since else ""
            head = f"STOPPED{since}"
        else:
            head = self.state
        why = self.reason or (self.outcome if self.state in ("stopped", "retrying") else None)
        branch = self.current_branch if self.reason == "off_branch" else None
        detail = " ".join(d for d in (why, branch) if d)
        lines = [f"{self.name}: {head}" + (f", {detail}" if detail else "")]
        if self.step:
            since = f" since {relative_time(self.step_started_at)}" if self.step_started_at else ""
            lines.append(f"running {self.step}{since}")
        if self.current_branch and self.state != "off":
            facts = [f"branch {self.current_branch}"]
            if self.ahead is not None and self.behind is not None:
                facts.append(f"ahead {self.ahead}, behind {self.behind}")
            if self.watch:
                facts.append(f"watching {self.watch}")
            lines.append(", ".join(facts))
        lines.extend(x for x in (self.note, self.fix) if x)
        return "\n    ".join(part for item in lines for part in item.split("\n"))


def _context_output(sync: autosync.ContextSync) -> SyncContextOutput:
    e: Mapping[str, Any] = sync.entry or {}

    def text(field: str) -> str | None:
        value = e.get(field)
        return value if isinstance(value, str) else None

    def number(field: str) -> int | None:
        value = e.get(field)
        return value if isinstance(value, int) else None

    repair = cast("Mapping[str, Any] | None", e.get("last_repair"))
    return SyncContextOutput(
        name=sync.name,
        clone_key=sync.clone_key,
        clone_path=sync.clone_path,
        state=sync.state,
        outcome=text("outcome"),
        reason=text("reason"),
        error=text("error"),
        fix=sync.fix,
        note=sync.note,
        default_branch=text("default_branch"),
        current_branch=text("current_branch"),
        ahead=number("ahead"),
        behind=number("behind"),
        last_pass_at=text("last_pass_at"),
        last_success_at=text("last_success_at"),
        stopped_since=text("stopped_since"),
        step=text("step"),
        step_started_at=text("step_started_at"),
        watch=text("watch"),
        last_repair=(
            RepairOutput(
                from_branch=str(repair.get("from", "")),
                commits=int(repair.get("commits", 0)),
                at=str(repair.get("at", "")),
            )
            if isinstance(repair, Mapping)
            else None
        ),
    )


class DaemonOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    running: bool
    service_off: bool
    binary: str | None
    meridian_version: str
    pid: int | None = None
    version: str | None = None
    started_at: str | None = None
    alive_at: str | None = None
    scanned_at: str | None = None
    problem: str | None = None


class SyncStatusOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    daemon: DaemonOutput
    contexts: tuple[SyncContextOutput, ...]
    skipped_records: tuple[SkippedRecordOutput, ...] = ()

    def format_text(self, ctx: FormatContext | None = None) -> str:
        d = self.daemon
        if d.running:
            alive = f"alive {relative_time(d.alive_at)}" if d.alive_at else None
            parts = [
                f"pid {d.pid}" if d.pid else None,
                f"meridian-sync {d.version}" if d.version else None,
                alive,
            ]
            detail = ", ".join(x for x in parts if x)
            daemon = f"running ({detail})" if detail else "running"
        elif d.service_off:
            daemon = "off (autosync/service.off exists)"
        else:
            daemon = f"not running. {autosync.NOT_RUNNING_FIX}"
        pairs: list[tuple[str, str | None]] = [
            ("daemon", daemon),
            ("binary", d.binary or "meridian-sync is not installed"),
            ("meridian", d.meridian_version),
            ("state.json", d.problem),
        ]
        blocks = [kv_block(pairs)]
        blocks.extend(c.line() for c in self.contexts)
        if not self.contexts:
            blocks.append("This project has no git contexts.")
        for skipped in self.skipped_records:
            blocks.append(f"skipped record {skipped.file}: {skipped.error}")
        return "\n".join(blocks)


class SyncNowOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    contexts: tuple[SyncContextOutput, ...]
    timed_out: bool = False

    def format_text(self, ctx: FormatContext | None = None) -> str:
        lines = [c.line() for c in self.contexts]
        if self.timed_out:
            lines.append(f"Still running after {int(NOW_TIMEOUT_S)} s.")
        return "\n".join(lines)


def _project_root() -> Path:
    return resolve_project_root_resolution(None).project_root


def _daemon_output(file: autosync.DaemonFile, *, running: bool) -> DaemonOutput:
    pid = file.data.get("pid") if file.data else None
    return DaemonOutput(
        running=running,
        service_off=autosync.service_off(),
        binary=installed_executable("meridian-sync"),
        meridian_version=_meridian_version(),
        pid=pid if isinstance(pid, int) else None,
        version=file.text("version"),
        started_at=file.text("started_at"),
        alive_at=file.text("alive_at"),
        scanned_at=file.text("scanned_at"),
        problem=file.problem,
    )


def _meridian_version() -> str:
    from meridian import __version__

    return __version__


def sync_status_sync() -> SyncStatusOutput:
    root = _project_root()
    contexts = autosync.git_contexts(root)
    running = autosync.daemon_running()
    file = autosync.read_daemon_file()
    states = autosync.context_sync(
        contexts,
        running=running,
        service_is_off=autosync.service_off(),
        file=file,
        written_at=autosync.read_record_written_at(root),
        record_file=autosync.record_path(root).name,
    )
    return SyncStatusOutput(
        daemon=_daemon_output(file, running=running),
        contexts=tuple(_context_output(s) for s in states),
        skipped_records=tuple(
            SkippedRecordOutput(
                file=str(r.get("file", "")),
                v=r["v"] if isinstance(r.get("v"), int) else None,
                error=str(r.get("error", "")),
            )
            for r in file.skipped_records
        ),
    )


def _settled(sync: autosync.ContextSync, written_at: str) -> bool:
    if sync.entry is None:
        return sync.note != autosync.STARTING_NOTE
    if sync.state == "off":
        return True
    answered = sync.entry.get("answered")
    return isinstance(answered, str) and answered >= written_at


def sync_now_sync(context: str | None) -> SyncNowOutput:
    """Ask the daemon for a pass on the project's git contexts and wait for the answer."""

    root = _project_root()
    all_contexts = autosync.git_contexts(root)
    selected = [c for c in all_contexts if context is None or c.name == context.strip().lower()]
    if not selected:
        if not all_contexts:
            raise ValueError("This project has no git contexts.")
        names = ", ".join(c.name for c in all_contexts)
        raise ValueError(f"No git context named `{context}` (git contexts here: {names}).")
    written_at = autosync.register_project(root, force=True)
    if written_at is None:
        raise ValueError(f"`{root}` is inside a context clone, so it syncs nothing.")
    record_file = autosync.record_path(root).name

    deadline = time.monotonic() + NOW_TIMEOUT_S
    while True:
        running = autosync.daemon_running()
        file = autosync.read_daemon_file()
        states = autosync.context_sync(
            selected,
            running=running,
            service_is_off=autosync.service_off(),
            file=file,
            written_at=written_at,
            record_file=record_file,
        )
        done = not running or all(_settled(s, written_at) for s in states)
        timed_out = not done and time.monotonic() >= deadline
        if done or timed_out:
            return SyncNowOutput(
                ok=all(s.state == "synced" for s in states),
                contexts=tuple(_context_output(s) for s in states),
                timed_out=timed_out,
            )
        time.sleep(_POLL_S)


def run_daemon_sync() -> int:
    """Run `meridian-sync` in the foreground until it stops; start it again when it exits
    75 because an upgrade replaced its binary. Returns its exit code."""

    exe = installed_executable("meridian-sync")
    if exe is None:
        raise RuntimeError(
            "meridian-sync is not installed beside this meridian. Reinstall meridian-cli."
        )
    home = get_user_home().absolute()
    child: subprocess.Popen[bytes] | None = None
    stop_requested = False

    def forward(signum: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True
        if child is not None and child.poll() is None:
            child.send_signal(signal.SIGTERM)

    previous = {s: signal.signal(s, forward) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        while True:
            # The daemon detaches from this terminal itself, so a Ctrl-C reaches only us;
            # `forward` turns it into the daemon's SIGTERM.
            child = subprocess.Popen([exe, "run", "--home", str(home), "--no-wait"])
            if stop_requested:  # the signal came before there was a child to forward it to
                child.send_signal(signal.SIGTERM)
            code = child.wait()
            if code != _RESTART_CODE or stop_requested:
                return code
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
