import pytest

from meridian.lib.ops.session_reentry import (
    Blocked,
    Fork,
    Resume,
    SessionReentryDecision,
    decide_reentry,
)


@pytest.mark.parametrize(
    ("live", "has_harness_session", "expected"),
    [
        (True, False, Blocked("no harness session recorded; cannot resume or fork")),
        (True, True, Fork("c1")),
        (False, True, Resume("c1")),
    ],
)
def test_decide_reentry(
    live: bool,
    has_harness_session: bool,
    expected: SessionReentryDecision,
) -> None:
    assert (
        decide_reentry(
            chat_id="c1",
            live=live,
            has_harness_session=has_harness_session,
        )
        == expected
    )
