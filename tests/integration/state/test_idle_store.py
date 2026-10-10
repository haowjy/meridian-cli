from pathlib import Path

from meridian.lib.state.idle_store import IdleSchedule, IdleState, IdleStore

DAY_MS = 24 * 60 * 60 * 1000


def _state(*, updated_at_ms: int = 0) -> IdleState:
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
        updated_at_ms=updated_at_ms,
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


def test_idle_store_lazy_gc_removes_state_older_than_seven_days(tmp_path: Path) -> None:
    root = tmp_path / "idle"
    old_now = 10 * DAY_MS
    old_store = IdleStore(root, now_ms=lambda: old_now)
    old_store.write(_state())
    stale_path = old_store.path_for("example", "native-1")

    current_now = old_now + 8 * DAY_MS
    current_store = IdleStore(root, now_ms=lambda: current_now)
    current_store.write(_state().model_copy(update={"session": "native-2", "done": {}}))

    assert not stale_path.exists()
    assert not current_store._lock_path(stale_path).exists()
    assert current_store.read("example", "native-2") is not None
