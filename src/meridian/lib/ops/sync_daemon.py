"""`meridian sync run|now|status`: the foreground daemon and what it says."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict

from meridian.lib.autosync import service as autosync_service
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


class ConflictFileOutput(BaseModel):
    """One conflicting path with both sides' content (`None`: absent on that side)."""

    model_config = ConfigDict(frozen=True)

    path: str
    local: str | None = None
    remote: str | None = None


class ConflictOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    paths: tuple[str, ...]
    local_sha: str | None = None
    remote_sha: str | None = None
    since: str | None = None
    # Filled by `sync now`, which shows both sides.
    files: tuple[ConflictFileOutput, ...] = ()


class CloneSyncOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    contexts: tuple[str, ...]
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
    conflict: ConflictOutput | None = None

    def line(self) -> str:
        return f"{', '.join(self.contexts)}: {self.summary()}"

    def conflict_sides(self) -> list[str]:
        """Each conflicting file with the local and the remote content."""

        if self.conflict is None:
            return []
        local, remote = (self.conflict.local_sha or "")[:8], (self.conflict.remote_sha or "")[:8]
        lines: list[str] = []
        for f in self.conflict.files:
            lines.append(f"\n=== {f.path} ===")
            lines.append(f"--- local ({local}) ---")
            lines.append(f.local if f.local is not None else "(absent)")
            lines.append(f"--- remote ({remote}) ---")
            lines.append(f.remote if f.remote is not None else "(absent)")
        return lines

    def summary(self) -> str:
        """`synced 2m ago` / `STOPPED since …, off_branch`, then the running step, the
        branch, the conflicting paths and the fix, one per indented line."""

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
        lines = [head + (f", {detail}" if detail else "")]
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


def _clone_output(sync: autosync.CloneSync) -> CloneSyncOutput:
    e: Mapping[str, Any] = sync.entry or {}

    def text(field: str) -> str | None:
        value = e.get(field)
        return value if isinstance(value, str) else None

    def number(field: str) -> int | None:
        value = e.get(field)
        return value if isinstance(value, int) else None

    repair = cast("Mapping[str, Any] | None", e.get("last_repair"))
    conflict = cast("Mapping[str, Any] | None", e.get("conflict"))
    return CloneSyncOutput(
        contexts=sync.contexts,
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
        conflict=(
            ConflictOutput(
                paths=tuple(str(p) for p in conflict.get("paths", ())),
                local_sha=conflict.get("local_sha"),
                remote_sha=conflict.get("remote_sha"),
                since=conflict.get("since"),
            )
            if isinstance(conflict, Mapping)
            else None
        ),
    )


class DaemonOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    running: bool
    service_off: bool
    service: autosync_service.ServiceStatus
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
    clones: tuple[CloneSyncOutput, ...]
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
            daemon = f"not running. {autosync.not_running_fix()}"
        pairs: list[tuple[str, str | None]] = [
            ("daemon", daemon),
            ("service", d.service.text()),
            ("binary", d.binary or "meridian-sync is not installed"),
            ("meridian", d.meridian_version),
            ("state.json", d.problem),
        ]
        blocks = [kv_block(pairs)]
        blocks.extend(c.line() for c in self.clones)
        if not self.clones:
            blocks.append("This project has no git contexts.")
        for skipped in self.skipped_records:
            blocks.append(f"skipped record {skipped.file}: {skipped.error}")
        return "\n".join(blocks)


class SyncNowOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    clones: tuple[CloneSyncOutput, ...]
    timed_out: bool = False

    def format_text(self, ctx: FormatContext | None = None) -> str:
        lines: list[str] = []
        for c in self.clones:
            lines.append(c.line())
            lines.extend(c.conflict_sides())
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
        service=autosync_service.service_status(),
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


def _project_states(
    root: Path, contexts: Sequence[autosync.GitContext] | None = None
) -> tuple[bool, autosync.DaemonFile, list[autosync.CloneSync]]:
    running = autosync.daemon_running()
    file = autosync.read_daemon_file()
    states = autosync.clone_syncs(
        autosync.git_contexts(root) if contexts is None else contexts,
        running=running,
        service_is_off=autosync.service_off(),
        file=file,
        written_at=autosync.read_record_written_at(root),
        record_file=autosync.record_path(root).name,
    )
    return running, file, states


def project_sync_summaries(root: Path) -> dict[str, str]:
    """`meridian context`'s Sync section: each clone's state, by the names of the contexts
    in it. A config that doesn't load has no git contexts to show."""

    try:
        _, _, states = _project_states(root)
    except ValueError:
        return {}
    return {s.names: _clone_output(s).summary() for s in states}


def sync_status_sync() -> SyncStatusOutput:
    running, file, states = _project_states(_project_root())
    return SyncStatusOutput(
        daemon=_daemon_output(file, running=running),
        clones=tuple(_clone_output(s) for s in states),
        skipped_records=tuple(
            SkippedRecordOutput(
                file=str(r.get("file", "")),
                v=r["v"] if isinstance(r.get("v"), int) else None,
                error=str(r.get("error", "")),
            )
            for r in file.skipped_records
        ),
    )


_SIDE_LINES = 200


def _show(clone: str, sha: str, path: str) -> str | None:
    """`path` at `sha`, trimmed; `None` when that side has no such file."""

    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(Path(clone).parent), "LC_ALL": "C"}
    try:
        result = subprocess.run(
            ["git", "-C", clone, "show", f"{sha}:{path}"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=30,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    lines = result.stdout.splitlines()
    if len(lines) > _SIDE_LINES:
        lines = [*lines[:_SIDE_LINES], f"... ({len(lines) - _SIDE_LINES} more lines)"]
    return "\n".join(lines)


def _with_conflict_sides(out: CloneSyncOutput) -> CloneSyncOutput:
    c = out.conflict
    if c is None or not c.local_sha or not c.remote_sha:
        return out
    files = tuple(
        ConflictFileOutput(
            path=p,
            local=_show(out.clone_path, c.local_sha, p),
            remote=_show(out.clone_path, c.remote_sha, p),
        )
        for p in c.paths
    )
    return out.model_copy(update={"conflict": c.model_copy(update={"files": files})})


def _settled(sync: autosync.CloneSync, written_at: str) -> bool:
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
    wanted = None if context is None else context.strip().lower()
    if wanted is not None and wanted not in {c.name for c in all_contexts}:
        if not all_contexts:
            raise ValueError("This project has no git contexts.")
        names = ", ".join(c.name for c in all_contexts)
        raise ValueError(f"No git context named `{context}` (git contexts here: {names}).")
    registration = autosync.register_project(root, force=True)
    if registration is None:
        raise ValueError(f"`{root}` is inside a context clone, so it syncs nothing.")
    written_at = registration.written_at
    record_file = autosync.record_path(root).name

    deadline = time.monotonic() + NOW_TIMEOUT_S
    while True:
        running = autosync.daemon_running()
        file = autosync.read_daemon_file()
        # Every context decides its clone's `autosync`, so the rows come from all of them.
        states = [
            s
            for s in autosync.clone_syncs(
                all_contexts,
                running=running,
                service_is_off=autosync.service_off(),
                file=file,
                written_at=written_at,
                record_file=record_file,
            )
            if wanted is None or wanted in s.contexts
        ]
        done = not running or all(_settled(s, written_at) for s in states)
        timed_out = not done and time.monotonic() >= deadline
        if done or timed_out:
            return SyncNowOutput(
                ok=all(s.state == "synced" for s in states),
                clones=tuple(_with_conflict_sides(_clone_output(s)) for s in states),
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


# ---- The service, and what a launch says --------------------------------------------


def service_install_sync() -> autosync_service.ServiceResult:
    return autosync_service.install_service()


def service_uninstall_sync() -> autosync_service.ServiceResult:
    return autosync_service.uninstall_service()


def _stopped_line(s: autosync.CloneSync) -> str:
    """One line: the clone's contexts, why it stopped and what to do."""

    out = _clone_output(s)
    why = out.reason or out.outcome or out.note
    since = f" since {relative_time(out.stopped_since)}" if out.stopped_since else ""
    fix = f" {out.fix}" if out.fix else ""
    return f"{s.names}: STOPPED{since}{f', {why}' if why else ''}.{fix}"


def startup_notices(project_root: Path, registration: autosync.Registration) -> list[str]:
    """What a human's primary launch prints before the harness starts. When the service was
    just installed or started, that and nothing else: the daemon needs a moment to take
    its lock and a probe now would only guess. Otherwise one line per stopped clone, or one
    that no daemon is running. Nothing when all is well or off. Never raises: syncing is
    not the launch."""

    if not any(c.autosync for c in registration.contexts):
        return []
    try:
        result = autosync_service.ensure_service(auto=True)
    except (autosync_service.ServiceError, OSError) as exc:
        return [f"autosync: the sync service could not be started: {exc}"]
    if result.action == "installed":
        return [
            f"autosync: installed the sync service `{result.unit}` for your git contexts. "
            "Turn it off with `meridian sync service uninstall`."
        ]
    if result.action in ("updated", "started"):
        return [
            f"autosync: started the sync service `{result.unit}`; if this repeats, "
            f"look at `{result.log}`."
        ]
    _, _, states = _project_states(project_root, registration.contexts)
    # The record was just written; `written_at` is what the daemon must have answered.
    notices = [f"autosync: {_stopped_line(s)}" for s in states if s.state == "stopped"]
    if any(s.note == autosync.NOT_RUNNING_NOTE for s in states):
        notices.append(f"autosync: daemon not running. {autosync.not_running_fix()}")
    return notices


def doctor_findings(root: Path) -> tuple[str | None, list[tuple[str, str]]]:
    """`meridian doctor`'s view of autosync: a one-line status (`None` when the project has
    no git contexts) and `(code, message)` problems, all read from the clone rows."""

    try:
        contexts = autosync.git_contexts(root)
    except ValueError:
        return None, []
    if not contexts:
        return None, []
    running, file, states = _project_states(root, contexts)
    problems: list[tuple[str, str]] = []
    if not running:
        if autosync.service_off():
            return "off (autosync/service.off exists)", []
        if any(s.note == autosync.NOT_RUNNING_NOTE for s in states):
            problems.append(
                (
                    "autosync_daemon_not_running",
                    f"The sync daemon is not running. {autosync.not_running_fix()}",
                )
            )
            service = autosync_service.service_status()
            if service.manager is not None and not service.installed:
                problems.append(("autosync_service_missing", "The sync service is not installed."))
            return "daemon not running", problems
        return "off (no context has autosync on)", []
    for s in states:
        out = _clone_output(s)
        if s.state == "stopped":
            problems.append(("autosync_stopped", _stopped_line(s)))
        if out.reason in ("git_missing", "git_too_old"):
            problems.append(("autosync_git", f"{s.names}: {out.reason}. {out.error or ''}".strip()))
        if out.watch == "polling":
            problems.append(
                (
                    "autosync_polling",
                    f"{s.names}: watching by polling (too many directories or "
                    "the inotify limit); changes sync up to 15 s late.",
                )
            )
    if file.problem:
        problems.append(("autosync_state_unreadable", file.problem))
    return "running", problems
