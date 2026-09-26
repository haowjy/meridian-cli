"""One-time exact native-key import; module entry point is strictly read-only.

Dev report: python -m meridian.lib.ops.legacy_native_import RUNTIME_ROOT
This bypasses CLI startup, telemetry, indexes and automatic migration entirely.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from meridian.lib.core.native_identity import NativeKeyFields
from meridian.lib.core.types import ChatId
from meridian.lib.harness.legacy_native_stores import SUPPORTED, LegacyNativeStores
from meridian.lib.platform.locking import lock_file
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.event_store import read_events, utc_now_iso
from meridian.lib.state.native_binding import Conflict
from meridian.lib.state.session_binding import session_bindings
from meridian.lib.state.session_store import SessionRecord, list_all_session_records
from meridian.lib.state.spawn.repository import SpawnStateQuarantined, read_state, scan_spawn_ids

MARKER = "legacy-native-import-v1.json"
DEFERRAL_NOTE = "legacy-native-import-deferral.json"
RETRY_DELAY_SECONDS = 15 * 60
REASONS = ("imported", "missing", "ambiguous", "ambiguous_id", "no_session_id", "unsupported")


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
    late_retries: list[str]

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
        try:
            prior = ImportReport.model_validate_json(marker.read_text(encoding="utf-8"))
        except (OSError, ValidationError):
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
