"""One-time exact native-key import; module entry point is strictly read-only.

Dev report: python -m meridian.lib.ops.legacy_native_import RUNTIME_ROOT
This bypasses CLI startup, telemetry, indexes and automatic migration entirely.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from meridian.lib.harness.legacy_native_stores import SUPPORTED, LegacyNativeStores
from meridian.lib.platform.locking import lock_file
from meridian.lib.state.atomic import atomic_write_text
from meridian.lib.state.event_store import read_events, utc_now_iso
from meridian.lib.state.session_binding import session_bindings
from meridian.lib.state.session_store import SessionRecord, list_all_session_records
from meridian.lib.state.spawn.repository import read_state, scan_spawn_ids

MARKER = "legacy-native-import-v1.json"
REASONS = ("imported", "missing", "ambiguous", "ambiguous_id", "no_session_id", "unsupported")


@dataclass
class ImportReport:
    schema_version: int = 1
    timestamp: str = field(default_factory=utc_now_iso)
    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    unbound: dict[str, list[str]] = field(default_factory=dict)
    bindings: dict[str, tuple[str, str]] = field(default_factory=dict)

    def record(self, chat: SessionRecord, reason: str) -> None:
        self.counts.setdefault(chat.harness, dict.fromkeys(REASONS, 0))[reason] += 1
        if reason != "imported":
            self.unbound.setdefault(reason, []).append(chat.chat_id)

    def json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"


def report_legacy_native_import(
    runtime_root: Path,
    *,
    records: list[SessionRecord] | None = None,
) -> ImportReport:
    """Compute exact bindings without locks, state writes, or native-store writes."""
    report = ImportReport()
    chats = records if records is not None else list_all_session_records(runtime_root)
    ids: dict[str, set[str]] = defaultdict(set)
    # Raw historical arrays matter here even though normal replay ignores them.
    for event in read_events(runtime_root / "sessions.jsonl", lambda row: row):
        fact: dict[str, Any] = event.get("record", event)
        chat_id = fact.get("chat_id")
        if not chat_id:
            continue
        for value in [fact.get("harness_session_id"), *fact.get("harness_session_ids", [])]:
            if isinstance(value, str) and value.strip():
                ids[chat_id].add(value.strip())
    spawns = defaultdict(list)
    for spawn_id in scan_spawn_ids(runtime_root / "spawns"):
        spawn = read_state(runtime_root / "spawns", spawn_id, include_prompt=False)
        if spawn is not None and spawn.chat_id:
            spawns[spawn.chat_id].append(spawn)
            if spawn.harness_session_id:
                ids[spawn.chat_id].add(spawn.harness_session_id)
    with ExitStack() as scratch:
        stores = LegacyNativeStores(scratch)
        for chat in chats:
            if chat.native_store and chat.harness_session_id:
                continue
            if chat.harness not in SUPPORTED or chat.record_mode == "historical":
                report.record(chat, "unsupported")
            elif len(ids[chat.chat_id]) > 1:
                report.record(chat, "ambiguous_id")
            elif not ids[chat.chat_id]:
                report.record(chat, "no_session_id")
            else:
                session_id = next(iter(ids[chat.chat_id]))
                matches, ambiguous = stores.matching_stores(chat, spawns[chat.chat_id], session_id)
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
    if marker.exists():
        return None
    # Do not create runtime state for an untouched project.
    if not (runtime_root / "sessions.jsonl").exists():
        return None
    with lock_file(runtime_root / "locks" / "legacy-native-import.lock"):
        if marker.exists():
            return None
        with session_bindings(runtime_root) as bindings:
            report = report_legacy_native_import(
                runtime_root, records=list(bindings.records.values())
            )
            for chat_id, (session_id, store) in report.bindings.items():
                result = bindings.bind(
                    chat_id,
                    session_id,
                    native_store=store,
                    source="legacy_import",
                    session_instance_id=bindings.records[chat_id].session_instance_id,
                )
                if result.status == "conflict":
                    raise ValueError(f"Legacy import binding conflict: {chat_id}")
            atomic_write_text(marker, report.json())
        imported = len(report.bindings)
        total = sum(sum(counts.values()) for counts in report.counts.values())
        print(
            f"Imported native sessions for {imported} of {total} existing chats; "
            f"{total - imported} left unbound (details: {marker})",
            file=sys.stderr,
        )
        return report


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Read-only legacy native-session import report")
    parser.add_argument("runtime_root", type=Path)
    args = parser.parse_args()
    # Existing legacy identity-conflict diagnostics belong on stderr, not in JSON.
    import structlog

    structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))
    print(report_legacy_native_import(args.runtime_root).json(), end="")
