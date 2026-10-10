from pathlib import Path

from meridian.lib.state.idle_store import IdleSchedule, IdleState, IdleStore


def _state() -> IdleState:
    return IdleState(
        harness="example",
        session="native-1",
        stretch=12,
        stretch_open=True,
        last_turn_id="turn-1",
        anchor=3,
        idle_since_ms=1_760_000_000_000,
        ttl_seconds=3600,
        schedule=IdleSchedule(
            push_at=1_760_000_060_000,
            warn_at=1_760_002_700_000,
            compact_at=1_760_003_300_000,
        ),
        done={"push": "sent", "compact": "claimed"},
        compact_window_until_ms=1_760_003_330_000,
        expect_compaction_turn=True,
        updated_at_ms=0,
    )


def test_idle_store_round_trips_versioned_json(tmp_path: Path) -> None:
    now_ms = 1_760_003_300_000
    store = IdleStore(tmp_path / "idle", now_ms=lambda: now_ms)

    written = store.write(_state())

    assert written.updated_at_ms == now_ms
    assert store.read("example", "native-1") == written
    assert store.path_for("example", "native-1").name == "example-native-1.json"
    assert (
        store.path_for("example", "native-1").read_text(encoding="utf-8").startswith('{\n  "v": 1,')
    )


def test_idle_store_treats_truncated_file_as_no_stretch(tmp_path: Path) -> None:
    store = IdleStore(tmp_path / "idle")
    path = store.path_for("example", "native-1")
    assert store.read("example", "native-1") is None

    path.parent.mkdir(parents=True)
    path.write_text('{"v": 1, "stretch":', encoding="utf-8")

    assert store.read("example", "native-1") is None
    assert store.list_states() == ()
