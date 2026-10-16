"""Sealed native snapshots for terminal spawn retention fixtures."""

import hashlib
import json
from pathlib import Path

from meridian.lib.core.refs import parse_ref
from meridian.lib.state import spawn_store
from meridian.lib.state.history_codec import transcript_header
from meridian.lib.state.native_snapshot import (
    SnapshotHeader,
    SnapshotObservation,
    SnapshotRecord,
    SourceRevision,
    write_snapshot,
)


def seal_spawn_snapshot(root: Path, key: str, *, text: str = "portable needle") -> None:
    row = spawn_store.get_spawn(root, parse_ref(key, row_key=True))
    assert row is not None and row.terminal is not None and row.started_at is not None
    raw = json.dumps(
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            },
        }
    )
    header = SnapshotHeader(
        transcript=transcript_header(row, root.name),
        session_instance_id=row.session_instance_id,
        harness="codex",
        native_session_id=row.harness_session_id or "native",
        dialect="codex.rollout.v1",
        scope="fixture",
        observed_from=row.started_at,
    )
    observation = SnapshotObservation(
        observed_until=row.terminal.finished_at,
        sources=(
            SourceRevision(
                source="codex",
                sha256=hashlib.sha256(raw.encode()).hexdigest(),
                records=1,
            ),
        ),
    )
    with (root / "spawns" / key / "native-transcript.jsonl").open("wb") as handle:
        write_snapshot(
            handle,
            header,
            (SnapshotRecord(source="codex", ordinal=0, raw=raw),),
            lambda: observation,
        )

