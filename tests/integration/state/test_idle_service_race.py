from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from meridian.lib.config.settings import MeridianConfig
from meridian.lib.harness.idle_types import IdleFacts
from meridian.lib.idle.service import IdleService
from meridian.lib.state.idle_store import IdleStore


def test_two_compaction_claims_racing_have_one_winner(tmp_path: Path) -> None:
    now = [0]
    store = IdleStore(tmp_path / "idle", now_ms=lambda: now[0])
    service = IdleService(
        store=store,
        config=MeridianConfig(),
        env={"MERIDIAN_SESSION_ROLE": "primary"},
        now_ms=lambda: now[0],
        spawn_reader=lambda: (),
    )
    armed = service.arm(harness="example", session="s1", ttl_seconds=3600)
    now[0] = armed.compact_at or 0
    facts = IdleFacts("no", False, 0, 100_000, False)

    def claim() -> str:
        return service.fire(
            "compact",
            harness="example",
            session="s1",
            stretch=1,
            anchor=1,
            facts=facts,
        ).decision

    def indexed_claim(_index: int) -> str:
        return claim()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(indexed_claim, range(2)))

    assert sorted(results) == ["act", "skip"]
    assert store.read("example", "s1").done == {"compact": "claimed"}  # type: ignore[union-attr]
