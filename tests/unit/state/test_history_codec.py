"""A JSONL append is committed only by its terminating newline."""

import pytest

from meridian.lib.state.history_codec import current_attempt_lines


@pytest.mark.parametrize("tail", ['{"seq":1', '{"seq":1}', 'partial output'])
def test_current_attempt_ignores_torn_final_record(tail: str) -> None:
    boundary = '{"event_type":"meridian.attempt.completed"}\n'
    assert current_attempt_lines(boundary + tail) == []
    assert current_attempt_lines(boundary + '{"seq":0}\n' + tail) == ['{"seq":0}']
