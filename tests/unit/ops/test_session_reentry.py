from meridian.lib.ops.session_reentry import Blocked, Fork, Resume, decide_reentry


def test_decide_reentry_blocks_without_harness_session() -> None:
    assert decide_reentry(
        chat_id="c1", live=True, has_harness_session=False
    ) == Blocked("no harness session recorded; cannot resume or fork")


def test_decide_reentry_forks_live_session() -> None:
    assert decide_reentry(chat_id="c1", live=True, has_harness_session=True) == Fork("c1")


def test_decide_reentry_resumes_stopped_session() -> None:
    assert decide_reentry(chat_id="c1", live=False, has_harness_session=True) == Resume("c1")
