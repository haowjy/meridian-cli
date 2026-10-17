from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from meridian.lib.config.models import MeridianConfig
from meridian.lib.idle.service import IdleService
from meridian.lib.notify import SendReport
from meridian.lib.state.idle_store import IdleStore


def test_two_push_claims_racing_have_one_winner(tmp_path: Path) -> None:
    now = [0]
    store = IdleStore(tmp_path / "idle", now_ms=lambda: now[0])
    service = IdleService(
        store=store,
        config=MeridianConfig(),
        env={"MERIDIAN_SESSION_ROLE": "primary"},
        now_ms=lambda: now[0],
        notify_sender=lambda _notice: SendReport(results=()),
    )
    armed = service.arm(harness="example", session="s1", ttl_seconds=3600)
    now[0] = armed.push_at or 0

    def claim() -> str:
        return service.fire(
            "push",
            harness="example",
            session="s1",
            stretch=1,
            anchor=1,
        ).decision

    def indexed_claim(_index: int) -> str:
        return claim()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(indexed_claim, range(2)))

    assert sorted(results) == ["act", "skip"]
    assert store.read("example", "s1").done["push"] in {"sent", "failed"}  # type: ignore[union-attr]
