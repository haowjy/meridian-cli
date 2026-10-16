"""B1's read/write oracle. Real-copy runner uses the same comparisons."""

import json
from pathlib import Path
from typing import get_args

import pytest

from meridian.lib.core.native_identity import NativeKeyFields
from meridian.lib.core.types import HarnessSessionId
from meridian.lib.state import session_binding, session_identity
from meridian.lib.state import session_store as s
from meridian.lib.state.native_binding import Bound, Conflict
from meridian.lib.state.spawn.model import SpawnRecord
from tests.support import session_lookup_oracle as old
from tests.support.session_corpus import generated_session_events
from tests.support.session_lookup_equivalence import compare_reads


def test_generated_lookup_equivalence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(s, "utc_now_iso", lambda: "2026-10-16T00:00:00Z")
    monkeypatch.setattr(old, "utc_now_iso", lambda: "2026-10-16T00:00:00Z")
    for seed in range(25):
        rows = generated_session_events(seed)
        # The fold generator deliberately poisons c3. Isolation of poisoned
        # chats is MEM-1's explicit behavior change, not a B1 equivalence case.
        rows = [row for row in rows if row.get("chat_id") != "c3"]
        for index, row in enumerate(rows):
            if row.get("event") == "start":
                row["started_at"] = (
                    f"2026-10-15T00:00:{(index // 2 if seed % 2 else 59 - index):02}Z"
                )
        for i in range(4, 7):
            rows.append(
                s.SessionStartEvent(
                    chat_id=f"c{i}",
                    harness="codex",
                    harness_session_id="alias" if i != 5 else None,
                    native_store="/native",
                    session_instance_id=f"g{i}",
                    model=f"model-{i}",
                    started_at=f"2026-10-16T00:00:0{i}Z",
                    spawn_id=f"p{i}",
                ).model_dump(mode="json")
            )
            rows.append(
                s.SessionModelSelectionEvent(
                    kind="invocation_started",
                    chat_id=f"c{i}",
                    harness="codex",
                    harness_session_id="alias" if i != 5 else None,
                    session_instance_id=f"g{i}",
                    startup_attempt_id="a",
                    spawn_id=f"p{i}",
                    recorded_at=f"2026-10-16T00:01:0{i}Z",
                    selection=s.ConversationModelSelection(
                        selection_source="unknown",
                        model_mode="harness_default",
                        requested_token=f"m{i}",
                    ),
                ).model_dump(mode="json")
            )
        rows.append(
            s.SessionUpdateEvent(
                chat_id="c5",
                session_instance_id="g5",
                startup_attempt_id="a",
                harness_session_id="alias",
            ).model_dump(mode="json")
        )
        rows.append(
            {"event": "unknown", "chat_id": "c4", "harness_session_ids": ["legacy", "alias"]}
        )
        rows.append(
            s.SessionModelObservationEvent(
                harness="codex",
                harness_session_id="alias",
                observed_model_token="executed",
                recorded_at="2026-10-16T00:02:00Z",
            ).model_dump(mode="json")
        )
        (tmp_path / "sessions.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
        identities = {
            ("codex", native) for native in ("alias", "legacy", "id1", "id2", "historical")
        }
        identities |= {("claude", "id1"), ("claude", "id2")}
        compare_reads(tmp_path, [f"c{i}" for i in range(1, 8)], identities)
        records = [SpawnRecord(id=f"p{i}", chat_id=f"c{i}") for i in range(4, 7)]
        assert session_identity.session_records_for_spawns(
            tmp_path, records
        ) == old.session_records_for_spawns(tmp_path, records)


def test_observed_alias_order_and_selection_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows: list[s.SessionEvent] = [
        s.SessionStartEvent(
            chat_id=chat,
            harness="codex",
            harness_session_id=native,
            native_store="/native",
            session_instance_id=generation,
            model="model",
            started_at=time,
        )
        for chat, generation, native, time in (
            ("c1", "g1", "source", "01"),
            ("c2", "g2", "alias", "02"),
            ("c3", "g3", "alias", "03"),
            ("c2", "g4", "alias", "04"),
        )
    ]
    rows.append(
        s.SessionModelSelectionEvent(
            kind="invocation_started",
            chat_id="c2",
            session_instance_id="g4",
            spawn_id="p2",
            startup_attempt_id="first",
            harness="codex",
            harness_session_id="alias",
            recorded_at="05",
            selection=s.ConversationModelSelection(
                selection_source="unknown", model_mode="harness_default", requested_token="chosen"
            ),
        )
    )
    (tmp_path / "sessions.jsonl").write_text("".join(row.model_dump_json() + "\n" for row in rows))
    expected = old._get_or_create_observed_chat_locked(
        tmp_path, "c1", harness="codex", native_store="/native", session_id="alias"
    )
    assert expected == "c2"
    assert (
        s._get_or_create_observed_chat_locked(
            tmp_path, "c1", harness="codex", native_store="/native", session_id="alias"
        )
        == expected
    )
    for spawn, attempt in (("p2", "first"), ("p2", "retry"), ("p3", "new")):
        event = s.SessionModelSelectionEvent(
            kind="invocation_started",
            chat_id="c2",
            session_instance_id="g4",
            spawn_id=spawn,
            startup_attempt_id=attempt,
            harness="codex",
            harness_session_id="alias",
            recorded_at="06",
            selection=s.ConversationModelSelection(
                selection_source="unknown", model_mode="harness_default", requested_token="new"
            ),
        )
        expected_rows: list[object] = []
        actual_rows: list[object] = []
        with monkeypatch.context() as patch:
            patch.setattr(
                old,
                "append_event",
                lambda _path, _lock, event, output=expected_rows, **_: output.append(event),
            )
            patch.setattr(
                s,
                "append_chat_rows",
                lambda _root, batches, output=actual_rows: output.extend(
                    event for rows in batches.values() for event in rows
                ),
            )
            assert s.record_model_selection(tmp_path, event) == old.record_model_selection(
                tmp_path, event
            )
        assert actual_rows == expected_rows


def test_native_ids_covers_model_fields_and_raw_legacy_rows(tmp_path: Path) -> None:
    models = (*get_args(s.SessionEvent.__value__), s.SessionModelObservationEvent, s.SessionRecord)
    fields = {
        model.__name__: {
            name for name, field in model.model_fields.items()
            if HarnessSessionId in (field.annotation, *get_args(field.annotation))
        }
        for model in models
    }
    assert fields == {
        "SessionStartEvent": {"harness_session_id"},
        "SessionStopEvent": set(),
        "SessionUpdateEvent": {"harness_session_id"},
        "SessionHistoricalEvent": set(),
        "SessionModelSelectionEvent": {"harness_session_id"},
        "SessionModelObservationEvent": {"harness_session_id"},
        "SessionRecord": {"harness_session_id"},
    }
    golden = [
        {
            "event": "start",
            "chat_id": " c1 ",
            "harness_session_id": " native ",
            "harness_session_ids": ["old", "", 7],
        },
        {"event": "update", "chat_id": "c2", "harness_session_id": "native"},
        {"event": "model_selection", "chat_id": "c3", "harness_session_id": "native"},
        {
            "event": "historical_import",
            "record": {
                "chat_id": "c4",
                "harness_session_id": "native",
                "harness_session_ids": ["old"],
            },
        },
    ]
    assert [s.native_ids(row) for row in golden] == [
        frozenset({"native", "old"}),
        frozenset({"native"}),
        frozenset({"native"}),
        frozenset({"native", "old"}),
    ]
    text = "".join(json.dumps(row) + "\n" for row in golden)
    # Native prefilter, like MEM-1's chat filter, nominates escaped keys/values.
    text += '{"event":"update","chat_id":"c5","harness_sessio\\u006e_id":"nati\\u0076e"}\n'
    text += '{"chat_id":"c6","harness_session_id":"native","harness_session_id":"not-native"}\n'
    (tmp_path / "sessions.jsonl").write_text(text)
    assert s.native_chats(tmp_path, "native") == ("c1", "c2", "c3", "c4", "c5")
    assert s.native_chats(tmp_path, "old") == ("c1", "c4")


def test_repair_bind_requires_full_scope_and_unique_native_id(tmp_path: Path) -> None:
    for chat, native, harness in (("c1", "native", "codex"), ("c2", "", "codex"), ("c3", "", "pi")):
        started = s.start_session(tmp_path, harness, native, "model", chat_id=chat)
        assert started.generation == s.chat_rows(tmp_path, chat)[-1].session_instance_id
        assert started.record == s.chat_record(tmp_path, chat)
        s.stop_session(tmp_path, chat)
    key = NativeKeyFields("codex", "/native", "native")
    for source in ("user_repair", "legacy_pi_recovery"):
        with (
            session_binding.session_bindings(tmp_path, scope={"c2"}) as bindings,
            pytest.raises(ValueError, match="full"),
        ):
            bindings.bind("c2", key, source=source)
        with session_binding.session_bindings(tmp_path) as bindings:
            assert isinstance(bindings.bind("c2", key, source=source), Conflict)
    with session_binding.session_bindings(tmp_path) as bindings:
        assert isinstance(
            bindings.bind(
                "c3", NativeKeyFields("pi", "/native", "native"), source="legacy_pi_recovery"
            ),
            Conflict,
        )
        assert isinstance(bindings.bind("c2", key, source="observed"), Bound)


def test_unbound_witness_stays_in_named_chat(tmp_path: Path) -> None:
    rows = [
        s.SessionStartEvent(
            chat_id=chat,
            harness="codex",
            harness_session_id=chat,
            session_instance_id=chat,
            spawn_id="p1",
            model="model",
            started_at=chat,
        )
        for chat in ("c1", "c2")
    ]
    (tmp_path / "sessions.jsonl").write_text("".join(row.model_dump_json() + "\n" for row in rows))
    for status in ("queued", "succeeded"):
        row = SpawnRecord(id="p1", chat_id="c1", status=status)
        assert session_identity.session_records_for_spawns(tmp_path, [row])["p1"].chat_id == "c1"


def test_eager_import_thread_covers_full_close(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    script = """
import sys
from pathlib import Path
from tests.support.session_close_probe import prepare_close, close
from meridian.lib.launch.session_scope import warm_close_imports
root = Path(sys.argv[1])
project = root / "project"
key = prepare_close(project, root / "home" / "projects" / "close-probe")
thread = warm_close_imports()
assert thread.daemon
thread.join(30)
assert not thread.is_alive()
before = set(sys.modules)
close(project, root / "home" / "projects" / "close-probe", key)
late = sorted(name for name in set(sys.modules) - before if name.startswith("meridian."))
assert not late, late
"""
    env = dict(os.environ, MERIDIAN_HOME=str(tmp_path / "home"))
    for name in ("_MERIDIAN_DEPTH", "MERIDIAN_SPAWN_ID", "MERIDIAN_CHAT_ID", "MERIDIAN_RUN_ID"):
        env.pop(name, None)
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("case", ["A", "B", "C", "D", "D2"])
def test_review_ordering_corpora(
    tmp_path: Path, case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """p8288: log order wins over chat numbers, timestamp ties and clock steps."""

    def start(chat, time, native="alias"):
        return s.SessionStartEvent(
            chat_id=chat,
            session_instance_id=chat,
            harness="codex",
            harness_session_id=native,
            native_store="/native",
            model=f"from-{chat}",
            started_at=time,
        )

    def selection(chat, time, spawn):
        return s.SessionModelSelectionEvent(
            kind="invocation_started",
            chat_id=chat,
            session_instance_id=chat,
            spawn_id=spawn,
            startup_attempt_id=spawn,
            harness="codex",
            harness_session_id="alias",
            recorded_at=time,
            selection=s.ConversationModelSelection(
                selection_source="unknown",
                requested_token=f"from-{spawn}",
                model_mode="harness_default",
            ),
        )

    if case == "A":
        rows = [
            start("c9", "01"),
            start("c10", "01"),
            selection("c10", "02", "c10"),
            selection("c9", "02", "c9"),
        ]
    elif case == "B":
        rows = [start("c9", "01"), selection("c9", "03", "first"), selection("c9", "02", "last")]
    elif case == "C":
        rows = [start("c5", "02"), start("c3", "02")]
    else:
        rows = [
            start("c1", "01", "source"),
            start("c6", "03"),
            start("c5", "03" if case == "D" else "02"),
        ]
    (tmp_path / "sessions.jsonl").write_text("".join(row.model_dump_json() + "\n" for row in rows))
    monkeypatch.setattr(s, "utc_now_iso", lambda: "now")
    monkeypatch.setattr(old, "utc_now_iso", lambda: "now")
    if case in {"A", "B"}:
        assert s.get_model_selection(tmp_path, "codex", "alias") == old.get_model_selection(
            tmp_path, "codex", "alias"
        )
    elif case == "C":
        assert s.get_initial_model_selection(
            tmp_path, "codex", "alias"
        ) == old.get_initial_model_selection(tmp_path, "codex", "alias")
    else:
        kwargs = dict(harness="codex", native_store="/native", session_id="alias")
        assert s._get_or_create_observed_chat_locked(
            tmp_path, "c1", **kwargs
        ) == old._get_or_create_observed_chat_locked(tmp_path, "c1", **kwargs)


def test_recent_sessions_reused_generation_seed_42(tmp_path: Path) -> None:
    from meridian.lib.state.history_index import HistoryIndex

    rows = [
        row
        for row in generated_session_events(42)
        if row.get("chat_id") != "c3" and row.get("event") != "historical_import"
    ]
    for index, row in enumerate(rows):
        if row["event"] == "start":
            row.update(kind="primary", started_at=f"2026-10-15T00:00:{index:02}Z")
    (tmp_path / "sessions.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    # Main selects the highest-ordinal bucket, not the accepted generation.
    # Frozen fold order and main's seed-42 browse order pin complete payloads.
    expected = {record.chat_id: record for record in old.list_session_generations(tmp_path)}
    actual = HistoryIndex(tmp_path).recent_sessions(limit=100, live_chat_ids=set())
    assert actual == ([expected["c1"], expected["c2"]], 2)
