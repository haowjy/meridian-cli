"""Parent-death linkage helpers for detached subprocess backends."""

from __future__ import annotations

import signal
from typing import TYPE_CHECKING

from meridian.lib.platform import detached_process
from tests.conftest import posix_only

if TYPE_CHECKING:
    import pytest


@posix_only
def test_detached_subprocess_config_links_linux_parent_death_preexec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(detached_process, "IS_WINDOWS", False)
    monkeypatch.setattr(detached_process, "_linux_parent_death_sig_available", lambda: True)
    calls: list[signal.Signals] = []

    def _set_parent_death_signal(signum: signal.Signals) -> None:
        calls.append(signum)

    monkeypatch.setattr(
        detached_process,
        "_set_parent_death_signal",
        _set_parent_death_signal,
    )
    monkeypatch.setattr(detached_process.os, "getppid", lambda: 4242)

    config = detached_process.detached_subprocess_config()
    preexec = config.kwargs["preexec_fn"]

    assert config.parent_death_linked is True
    assert config.kwargs["start_new_session"] is True
    assert callable(preexec)
    preexec()
    assert calls == [signal.SIGKILL]


def test_detached_subprocess_config_degrades_without_prctl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(detached_process, "IS_WINDOWS", False)
    monkeypatch.setattr(detached_process, "_linux_parent_death_sig_available", lambda: False)

    config = detached_process.detached_subprocess_config()

    assert config.parent_death_linked is False
    assert config.kwargs == {"start_new_session": True}
    assert "preexec_fn" not in config.kwargs


def test_link_child_lifetime_to_parent_posix_is_not_linked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _pgid_unavailable(_pid: int) -> None:
        return None

    monkeypatch.setattr(detached_process, "IS_WINDOWS", False)
    monkeypatch.setattr(detached_process, "_process_group_id", _pgid_unavailable)

    link = detached_process.link_child_lifetime_to_parent(4242)

    assert link == detached_process.ParentDeathLink(parent_death_linked=False)
