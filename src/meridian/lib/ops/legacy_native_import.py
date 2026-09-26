"""One-time exact native-key import; module entry point is strictly read-only.

Dev report: python -m meridian.lib.ops.legacy_native_import RUNTIME_ROOT
This bypasses CLI startup, telemetry, indexes and automatic migration entirely.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from meridian.lib.core.native_identity import NativeKeyFields, NativeSessionUnavailable
from meridian.lib.core.types import ChatId
from meridian.lib.harness.legacy_native_stores import (
    SUPPORTED,
    InvalidNativeSession,
    LegacyNativeStores,
    NativeSessionEvidence,
    parse_instant,
    pi_recorded_session_dir,
    pi_session_evidence,
    pi_sessions_in,
    pi_shared_session_root,
    pi_spawn_session_dir,
)
from meridian.lib.launch.constants import PI_RUNTIME_META_FILENAME
from meridian.lib.platform.locking import lock_file
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.event_store import read_events, utc_now_iso
from meridian.lib.state.native_binding import Conflict
from meridian.lib.state.retention_archive import read_archived_member, read_receipts
from meridian.lib.state.session_binding import session_bindings
from meridian.lib.state.session_store import SessionRecord, list_all_session_records
from meridian.lib.state.spawn.model import SpawnRecord
from meridian.lib.state.spawn.repository import (
    SpawnStateQuarantined,
    read_prompt,
    read_state,
    scan_spawn_ids,
)

MARKER = "legacy-native-import-v1.json"
DEFERRAL_NOTE = "legacy-native-import-deferral.json"
RETRY_DELAY_SECONDS = 15 * 60
REASONS = ("imported", "missing", "ambiguous", "ambiguous_id", "no_session_id", "unsupported")
logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class LateBinding:
    bound: int = 0
    attempted: int = 0


class ImportReport(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    schema_version: int
    timestamp: str
    counts: dict[str, dict[str, int]]
    unbound: dict[str, list[str]]
    bindings: dict[str, tuple[str, str]]
    late_retries: list[str] = Field(default_factory=list)
    # One-shot legacy Pi recovery: every no_session_id chat it examined. Markers
    # written before this pass existed default to [] and so get one pass.
    pi_recovery_tried: list[str] = Field(default_factory=list)

    def record(self, chat: SessionRecord, reason: str) -> None:
        self.counts.setdefault(chat.harness, dict.fromkeys(REASONS, 0))[reason] += 1
        if reason != "imported":
            self.unbound.setdefault(reason, []).append(chat.chat_id)

    def to_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def report_legacy_native_import(
    runtime_root: Path,
    *,
    records: list[SessionRecord] | None = None,
) -> ImportReport:
    """Compute exact bindings without locks, state writes, or native-store writes."""
    report = ImportReport(
        schema_version=1,
        timestamp=utc_now_iso(),
        counts={},
        unbound={},
        bindings={},
        late_retries=[],
    )
    chats = records if records is not None else list_all_session_records(runtime_root)
    ids: dict[str, set[str]] = defaultdict(set)
    cwds: dict[str, set[Path]] = defaultdict(set)
    previously_imported: set[str] = set()
    # Raw historical arrays matter here even though normal replay ignores them.
    for event in read_events(runtime_root / "sessions.jsonl", lambda row: row):
        fact = event.get("record", event)
        if not isinstance(fact, dict):
            continue
        chat_id = fact.get("chat_id")
        if not chat_id:
            continue
        for key in ("execution_cwd", "task_cwd", "control_root"):
            value = fact.get(key)
            if isinstance(value, str) and value:
                cwds[chat_id].add(Path(value))
        if event.get("source") == "legacy_import":
            previously_imported.add(chat_id)
        for value in [fact.get("harness_session_id"), *(fact.get("harness_session_ids") or [])]:
            if isinstance(value, str) and value.strip():
                ids[chat_id].add(value.strip())
    spawns = defaultdict(list)
    for spawn_id in scan_spawn_ids(runtime_root / "spawns"):
        spawn = read_state(runtime_root / "spawns", spawn_id, include_prompt=False)
        if spawn is not None and spawn.chat_id:
            spawns[spawn.chat_id].append(spawn)
            if spawn.harness_session_id:
                ids[spawn.chat_id].add(spawn.harness_session_id.strip())
    stores = LegacyNativeStores()
    for chat in chats:
        if chat.native_store and chat.harness_session_id:
            if chat.chat_id in previously_imported:
                report.record(chat, "imported")
            continue
        if chat.harness not in SUPPORTED or chat.record_mode == "historical":
            report.record(chat, "unsupported")
        elif len(ids[chat.chat_id]) > 1:
            report.record(chat, "ambiguous_id")
        elif not ids[chat.chat_id]:
            report.record(chat, "no_session_id")
        else:
            session_id = next(iter(ids[chat.chat_id]))
            matches, ambiguous = stores.matching_stores(
                chat, spawns[chat.chat_id], session_id, cwds[chat.chat_id]
            )
            if ambiguous or len(matches) > 1:
                report.record(chat, "ambiguous")
            elif not matches:
                report.record(chat, "missing")
            else:
                report.record(chat, "imported")
                report.bindings[chat.chat_id] = (session_id, str(next(iter(matches))))
    return report


def import_legacy_native_sessions(runtime_root: Path) -> ImportReport | None:
    """Once per runtime root; interrupted appends are skipped on the next run."""
    marker = runtime_root / MARKER
    deferral = runtime_root / DEFERRAL_NOTE
    if marker.exists():
        return None
    # Do not create runtime state for an untouched project.
    if not (runtime_root / "sessions.jsonl").exists():
        return None
    if _deferral_is_active(deferral):
        return None
    with lock_file(runtime_root / "locks" / "legacy-native-import.lock"):
        if marker.exists():
            return None
        if _deferral_is_active(deferral):
            return None
        records: dict[str, SessionRecord] = {
            chat.chat_id: chat for chat in list_all_session_records(runtime_root)
        }
        report = report_legacy_native_import(runtime_root, records=list(records.values()))
        with session_bindings(runtime_root) as bindings:
            # Native validation can be slow. Recheck identity under the sessions lock
            # without holding it over native I/O or the spawn scan.
            current_ids: dict[str, set[str]] = defaultdict(set)
            for event in bindings.events:
                session_id = getattr(event, "harness_session_id", None)
                if session_id:
                    current_ids[str(event.chat_id)].add(session_id)
            for chat_id, (session_id, store) in tuple(report.bindings.items()):
                original = records[chat_id]
                current = bindings.records.get(chat_id)
                if current is not None and current.native_store and current.harness_session_id:
                    # A concurrent launch already bound this chat; never touch it.
                    del report.bindings[chat_id]
                    report.counts[original.harness]["imported"] -= 1
                    continue
                if (
                    current is None
                    or len(current_ids[chat_id] | {session_id}) > 1
                    or current.session_instance_id != original.session_instance_id
                ):
                    del report.bindings[chat_id]
                    report.counts[original.harness]["imported"] -= 1
                    report.record(original, "ambiguous_id")
                    continue
                result = bindings.bind(
                    chat_id,
                    NativeKeyFields(original.harness, store, session_id),
                    source="legacy_import",
                    session_instance_id=original.session_instance_id,
                )
                if isinstance(result, Conflict):
                    del report.bindings[chat_id]
                    report.counts[original.harness]["imported"] -= 1
                    report.record(original, "ambiguous_id")
        atomic_write_text(marker, report.to_json())
        deferral.unlink(missing_ok=True)
        imported = sum(counts["imported"] for counts in report.counts.values())
        total = sum(sum(counts.values()) for counts in report.counts.values())
        print(
            f"Imported native sessions for {imported} of {total} existing chats; "
            f"{total - imported} left unbound (details: {marker})",
            file=sys.stderr,
        )
        return report


def bind_late_legacy_sessions(runtime_root: Path) -> LateBinding:
    """Repair late-arriving IDs listed by the completed one-time import."""
    marker = runtime_root / MARKER
    with lock_file(runtime_root / "locks" / "legacy-native-import.lock"):
        prior = _read_marker(marker)
        if prior is None:
            return LateBinding()
        no_session_id = prior.unbound.get("no_session_id")
        if no_session_id is None:
            return LateBinding()
        attempted = set(prior.late_retries)
        try:
            if (runtime_root / "sessions.jsonl").stat().st_mtime_ns <= marker.stat().st_mtime_ns:
                return LateBinding()
        except OSError:
            return LateBinding()

        records: dict[str, SessionRecord] = {
            str(record.chat_id): record for record in list_all_session_records(runtime_root)
        }
        candidates: dict[str, SessionRecord] = {
            chat_id: records[chat_id]
            for chat_id in no_session_id
            if chat_id not in attempted
            and chat_id in records
            and records[chat_id].native_key() is None
            and bool(records[chat_id].harness_session_id)
        }
        if not candidates:
            return LateBinding()

        # One raw journal scan for every candidate; the all-record fold above is
        # also shared rather than replaying sessions.jsonl once per chat.
        report = report_legacy_native_import(runtime_root, records=list(candidates.values()))
        bound = 0
        with session_bindings(runtime_root) as bindings:
            current_ids: dict[str, set[str]] = defaultdict(set)
            for event in bindings.events:
                session_id = getattr(event, "harness_session_id", None)
                if session_id:
                    current_ids[str(event.chat_id)].add(session_id)
            for chat_id, record in candidates.items():
                binding = report.bindings.get(chat_id)
                if binding is not None:
                    session_id, store = binding
                    current = bindings.records.get(ChatId(chat_id))
                    if (
                        current is not None
                        and current.native_key() is None
                        and current.harness_session_id == session_id
                        and len(current_ids[chat_id] | {session_id}) == 1
                        and current.session_instance_id == record.session_instance_id
                    ):
                        result = bindings.bind(
                            ChatId(chat_id),
                            NativeKeyFields(record.harness, store, session_id),
                            source="legacy_import",
                            session_instance_id=record.session_instance_id,
                        )
                        if not isinstance(result, Conflict):
                            no_session_id.remove(chat_id)
                            prior.bindings[chat_id] = (session_id, store)
                            bound += 1
                attempted.add(chat_id)

        prior.late_retries = sorted(attempted)
        atomic_write_text(marker, prior.to_json())
        return LateBinding(bound=bound, attempted=len(candidates))


def _read_marker(marker: Path) -> ImportReport | None:
    try:
        return ImportReport.model_validate_json(marker.read_text(encoding="utf-8"))
    except OSError:
        return None
    except ValidationError as exc:
        first = exc.errors()[0]
        logger.warning(
            "legacy_import_marker_invalid",
            marker_path=str(marker),
            field=".".join(str(part) for part in first["loc"]),
            error=first["msg"],
        )
        return None


# --- What Meridian retained about a chat ------------------------------------

TIME_SLACK = timedelta(seconds=120)


def normalize_text(text: str) -> str:
    return " ".join(text.split())


def report_body(report: str) -> str:
    """The assistant text inside a Meridian ``report.md`` (fallback heading stripped)."""
    lines = report.strip().splitlines()
    if lines and lines[0].strip() == "# Report":
        lines = lines[1:]
    if lines and lines[-1].startswith("[Attempt text was truncated"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


@dataclass
class RetainedChatFacts:
    """A chat's retained spawn mapping, cwds, time windows and proof texts."""

    spawn_ids: set[str] = field(default_factory=lambda: set[str]())
    session_dirs: set[Path] = field(default_factory=lambda: set[Path]())
    cwds: set[str] = field(default_factory=lambda: set[str]())
    windows: list[tuple[datetime, datetime]] = field(
        default_factory=lambda: list[tuple[datetime, datetime]]()
    )
    prompts: set[str] = field(default_factory=lambda: set[str]())  # whitespace-normalized
    reports: set[str] = field(default_factory=lambda: set[str]())  # normalized report bodies

    def add_cwds(self, *values: str | None) -> None:
        self.cwds.update(os.path.normpath(value) for value in values if value)

    def add_window(self, start: str | None, stop: str | None) -> None:
        begin = parse_instant(start)
        if begin is not None:
            # An unrecorded stop only admits sessions created near the start.
            end = parse_instant(stop) or begin
            self.windows.append((begin - TIME_SLACK, end + TIME_SLACK))

    def add_spawn(self, spawn: SpawnRecord) -> None:
        self.spawn_ids.add(str(spawn.id))
        self.add_cwds(spawn.execution_cwd, spawn.task_cwd, spawn.control_root)
        stop = spawn.terminal.finished_at if spawn.terminal else spawn.last_attempt_exited_at
        self.add_window(spawn.started_at, stop)

    def add_texts(self, prompt: str | None, report: str | None, runtime_meta: str | None) -> None:
        if prompt and prompt.strip():
            self.prompts.add(normalize_text(prompt))
        if report and report_body(report):
            self.reports.add(normalize_text(report_body(report)))
        session_dir = pi_recorded_session_dir(runtime_meta)
        if session_dir is not None:
            self.session_dirs.add(session_dir)

    def cwd_matches(self, cwd: str | None) -> bool:
        return cwd is not None and os.path.normpath(cwd) in self.cwds

    def time_matches(self, instant: datetime | None) -> bool:
        return instant is not None and any(lo <= instant <= hi for lo, hi in self.windows)

    def prompt_matches(self, evidence: NativeSessionEvidence) -> bool | None:
        """None when no prompt was retained; ambiguous retained prompts never match."""
        if not self.prompts:
            return None
        return (
            len(self.prompts) == 1
            and evidence.first_user is not None
            and normalize_text(evidence.first_user) in self.prompts
        )

    def content_proof(self, evidence: NativeSessionEvidence) -> bool:
        """First user message = retained prompt; else final assistant text = report body."""
        prompt = self.prompt_matches(evidence)
        if prompt is not None:
            return prompt
        return (
            len(self.reports) == 1
            and evidence.final_assistant is not None
            and normalize_text(evidence.final_assistant) in self.reports
        )


def configured_archive_destination(project_root: Path) -> Path | None:
    """The configured archive remount, so reclaimed spawns' retained files stay readable."""
    try:
        from meridian.lib.config.settings import load_config

        configured = load_config(project_root).history.archive.destination
    except Exception:
        return None
    return Path(configured).expanduser() if configured else None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def retained_chat_facts(
    runtime_root: Path,
    chats: dict[str, SessionRecord],
    *,
    archive_destination: Path | None = None,
) -> dict[str, RetainedChatFacts]:
    """Map chats to spawns through session records, spawn dirs and the archive catalog.

    Spawn dirs reclaimed by retention still map through ``sessions.jsonl``
    ``spawn_id`` and archive receipts; their prompt, report and Pi runtime
    metadata are read from the archived record when its ZIP is reachable.
    """
    facts = {chat_id: RetainedChatFacts() for chat_id in chats}
    if not facts:
        return facts
    for chat_id, record in chats.items():
        fact = facts[chat_id]
        fact.add_cwds(record.execution_cwd, record.task_cwd, record.control_root)
        fact.add_window(record.started_at, record.stopped_at)
        if record.spawn_id:
            fact.spawn_ids.add(record.spawn_id)
    # Raw journal rows keep cwds and spawn IDs that the fold replaced.
    for event in read_events(runtime_root / "sessions.jsonl", lambda row: row):
        raw = event.get("record", event)
        if not isinstance(raw, dict):
            continue
        row = cast("dict[str, object]", raw)
        fact = facts.get(str(row.get("chat_id")))
        if fact is None:
            continue
        fact.add_cwds(
            *(
                value
                for key in ("execution_cwd", "task_cwd", "control_root")
                if isinstance(value := row.get(key), str)
            )
        )
        if isinstance(spawn_id := row.get("spawn_id"), str) and spawn_id:
            fact.spawn_ids.add(spawn_id)
    spawns_dir = runtime_root / "spawns"
    local: set[str] = set()
    for spawn_id in scan_spawn_ids(spawns_dir):
        try:
            spawn = read_state(spawns_dir, spawn_id, include_prompt=False)
        except (SpawnStateQuarantined, OSError, ValueError):
            continue
        fact = facts.get(str(spawn.chat_id)) if spawn is not None and spawn.chat_id else None
        if spawn is None or fact is None:
            continue
        local.add(spawn_id)
        fact.add_spawn(spawn)
        directory = spawns_dir / spawn_id
        fact.add_texts(
            read_prompt(spawns_dir, spawn_id),
            _read_text(directory / "report.md"),
            _read_text(directory / PI_RUNTIME_META_FILENAME),
        )
    try:
        receipts = read_receipts(runtime_root)
    except (OSError, ValueError):
        receipts = ()
    archived: dict[UUID, SpawnRecord] = {}
    for receipt in receipts:
        for archived_record in receipt.records:
            state = archived_record.state
            if state.chat_id and str(state.chat_id) in facts and state.id not in local:
                archived.setdefault(archived_record.history_id, state)
    for history_id, state in archived.items():
        fact = facts[str(state.chat_id)]
        fact.add_spawn(state)
        texts = [
            read_archived_member(receipts, history_id, name, destination=archive_destination)
            for name in ("starting-prompt.md", "report.md", PI_RUNTIME_META_FILENAME)
        ]
        fact.add_texts(*(text.decode("utf-8", "replace") if text else None for text in texts))
    return facts


# --- One-shot legacy Pi recovery --------------------------------------------


@dataclass(frozen=True)
class PiRecovery:
    bound: int = 0
    unbound: tuple[str, ...] = ()


def _pi_spawn_candidates(fact: RetainedChatFacts) -> list[NativeSessionEvidence]:
    """Sessions in the chat's own spawn dirs (never the shared root) that fit cwd and time."""
    shared = pi_shared_session_root()
    directories = {pi_spawn_session_dir(spawn_id) for spawn_id in fact.spawn_ids}
    directories |= fact.session_dirs
    directories.discard(shared)
    return [
        evidence
        for directory in sorted(directories)
        for evidence in pi_sessions_in(directory)
        if fact.cwd_matches(evidence.cwd) and fact.time_matches(evidence.started_at)
    ]


def recover_legacy_pi_sessions(
    runtime_root: Path, *, archive_destination: Path | None = None
) -> PiRecovery:
    """Bind old 0.6.7 Pi chats whose native session is proven; one pass per marker.

    Proof: the chat's own spawn session dir holds exactly one valid Pi session
    with a recorded cwd, a header time inside the chat or spawn window, an ID no
    chat has bound or another chat also claims, and content that equals the
    retained starting prompt (or, with no prompt retained, the report body).
    Primaries shared one root, so they are never bound here.
    """
    marker = runtime_root / MARKER
    with lock_file(runtime_root / "locks" / "legacy-native-import.lock"):
        prior = _read_marker(marker)
        if prior is None:
            return PiRecovery()
        no_session_id = prior.unbound.get("no_session_id") or []
        tried = set(prior.pi_recovery_tried)
        pending = [chat_id for chat_id in no_session_id if chat_id not in tried]
        if not pending:
            return PiRecovery()
        records = {str(r.chat_id): r for r in list_all_session_records(runtime_root)}
        pi_chats = [
            chat_id
            for chat_id in pending
            if chat_id in records
            and records[chat_id].harness == "pi"
            and records[chat_id].native_key() is None
            and not records[chat_id].harness_session_id
        ]
        spawned = {
            chat_id: records[chat_id] for chat_id in pi_chats if records[chat_id].kind == "spawn"
        }
        facts = retained_chat_facts(runtime_root, spawned, archive_destination=archive_destination)
        bound_ids = {r.harness_session_id for r in records.values() if r.harness_session_id}
        survivors = {
            chat_id: {
                evidence.session_id: evidence
                for evidence in _pi_spawn_candidates(fact)
                if evidence.session_id not in bound_ids
            }
            for chat_id, fact in facts.items()
        }
        claims = Counter(sid for found in survivors.values() for sid in found)
        proven: dict[str, NativeSessionEvidence] = {}
        for chat_id, found in survivors.items():
            unique = [evidence for sid, evidence in found.items() if claims[sid] == 1]
            if len(unique) != 1:
                continue
            try:
                evidence = pi_session_evidence(unique[0].path, final_assistant=True)
            except (NativeSessionUnavailable, InvalidNativeSession, OSError):
                continue
            if evidence.session_id == unique[0].session_id and facts[chat_id].content_proof(
                evidence
            ):
                proven[chat_id] = evidence

        bound: set[str] = set()
        if proven:
            with session_bindings(runtime_root) as bindings:
                current_ids: set[str] = {
                    str(r.harness_session_id)
                    for r in bindings.records.values()
                    if r.harness_session_id
                }
                for chat_id, evidence in proven.items():
                    original = spawned[chat_id]
                    current = bindings.records.get(chat_id)
                    if (
                        current is None
                        or current.native_key() is not None
                        or current.harness_session_id
                        or current.session_instance_id != original.session_instance_id
                        or evidence.session_id in current_ids
                    ):
                        continue
                    store = str(evidence.native_store)
                    result = bindings.bind(
                        chat_id,
                        NativeKeyFields("pi", store, evidence.session_id),
                        source="legacy_pi_recovery",
                        session_instance_id=original.session_instance_id,
                    )
                    if isinstance(result, Conflict):
                        continue
                    current_ids.add(evidence.session_id)
                    no_session_id.remove(chat_id)
                    prior.bindings[chat_id] = (evidence.session_id, store)
                    bound.add(chat_id)
        prior.pi_recovery_tried = sorted(tried | set(pending))
        atomic_write_text(marker, prior.to_json())
        return PiRecovery(
            bound=len(bound),
            unbound=tuple(chat_id for chat_id in pi_chats if chat_id not in bound),
        )


def maybe_import_legacy_native_sessions(runtime_root: Path) -> None:
    """Keep a damaged source or unavailable filesystem from disabling the CLI."""
    try:
        import_legacy_native_sessions(runtime_root)
    except (SpawnStateQuarantined, OSError, sqlite3.Error) as exc:
        # Do not skip quarantined spawn rows: they might carry a conflicting ID.
        # A short backoff avoids repeating an expensive source scan on every command.
        with suppress(OSError):
            atomic_write_text(
                runtime_root / DEFERRAL_NOTE,
                json.dumps(
                    {"error": str(exc), "retry_after": time.time() + RETRY_DELAY_SECONDS},
                    sort_keys=True,
                )
                + "\n",
            )
        print(f"Native session import deferred for {runtime_root}: {exc}", file=sys.stderr)


def _deferral_is_active(path: Path) -> bool:
    """Read the tiny retry note once; malformed notes safely permit a retry."""
    try:
        note = json.loads(path.read_text(encoding="utf-8"))
        retry_after = note.get("retry_after") if isinstance(note, dict) else None
        return isinstance(retry_after, (int, float)) and retry_after > time.time()
    except (OSError, ValueError):
        return False


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Read-only legacy native-session import report")
    parser.add_argument("runtime_root", type=Path)
    args = parser.parse_args()
    # Existing legacy identity-conflict diagnostics belong on stderr, not in JSON.
    import structlog

    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))
    print(report_legacy_native_import(args.runtime_root).to_json(), end="")
