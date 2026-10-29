"""``fork_usage_seed``: a fork's usage baseline is only what its source stored for that session."""

from __future__ import annotations

from pathlib import Path

import pytest

from meridian.lib.launch import fork
from meridian.lib.launch.request import SessionRequest
from meridian.lib.state.spawn.model import PriorUsage, SpawnRecord

_ROOT = Path("/unused")
_STORED = {"input_tokens": 120}


def _source(
    *, harness_session_id: str = "native-1", prior: dict[str, object] | None = None
) -> SpawnRecord:
    return SpawnRecord(
        id="p1",
        status="running",
        harness_session_id=harness_session_id,
        prior_usage=PriorUsage(harness_cumulative=prior),
    )


def _fork(session_id: str = "native-1") -> SessionRequest:
    return SessionRequest(
        continue_fork=True,
        forked_from_chat_id="c1",
        requested_harness_session_id=session_id,
    )


@pytest.fixture
def stored_source(monkeypatch: pytest.MonkeyPatch):
    def install(source: SpawnRecord | None) -> None:
        monkeypatch.setattr(
            fork, "chat_owner_spawn", lambda _root, _chat: None if source is None else "p1"
        )
        monkeypatch.setattr(fork.spawn_store, "get_spawn", lambda _root, _id: source)

    return install


def test_seed_is_the_sources_stored_total_for_that_session(stored_source) -> None:
    stored_source(_source(prior=_STORED))
    assert fork.fork_usage_seed(_ROOT, _fork()) == _STORED


def test_a_different_session_of_the_source_counts_from_zero(stored_source) -> None:
    stored_source(_source(harness_session_id="native-2", prior=_STORED))
    assert fork.fork_usage_seed(_ROOT, _fork("native-1")) is None


def test_an_unknown_source_counts_from_zero(stored_source) -> None:
    stored_source(None)
    assert fork.fork_usage_seed(_ROOT, _fork()) is None
