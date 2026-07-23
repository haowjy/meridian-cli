"""Session log selector validation at the transcript I/O boundary."""

import json
from pathlib import Path

import pytest

from meridian.lib.ops.session_log import SessionLogInput, session_log_sync


@pytest.mark.parametrize(
    ("selectors", "message"),
    [
        (
            {"from_ordinal": 0, "before_ordinal": 1},
            "Use only one of --from, --before, or --around.",
        ),
        (
            {"from_ordinal": 0, "around_ordinal": 1},
            "Use only one of --from, --before, or --around.",
        ),
        (
            {"before_ordinal": 1, "around_ordinal": 0},
            "Use only one of --from, --before, or --around.",
        ),
        (
            {"global_scope": True, "segment": "current"},
            "--global cannot be combined with --segment.",
        ),
        (
            {"tail": 1, "from_ordinal": 0, "limit": 1},
            "--tail cannot be combined with --from/--before/--around.",
        ),
        (
            {"tail": 1, "before_ordinal": 1, "limit": 1},
            "--tail cannot be combined with --from/--before/--around.",
        ),
        (
            {"tail": 1, "around_ordinal": 0, "context": 1},
            "--tail cannot be combined with --from/--before/--around.",
        ),
        (
            {"full": True, "from_ordinal": 0, "limit": 1},
            "--full cannot be combined with --from/--before/--around.",
        ),
        (
            {"full": True, "before_ordinal": 1, "limit": 1},
            "--full cannot be combined with --from/--before/--around.",
        ),
        (
            {"full": True, "around_ordinal": 0, "context": 1},
            "--full cannot be combined with --from/--before/--around.",
        ),
        (
            {"full": True, "tail": 1},
            "--full cannot be combined with --tail.",
        ),
    ],
)
def test_session_log_rejects_conflicting_selectors(
    tmp_path: Path,
    selectors: dict[str, object],
    message: str,
) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "ready"}]},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    payload = SessionLogInput.model_validate(
        {"file_path": transcript.as_posix(), **selectors}
    )

    with pytest.raises(ValueError) as exc_info:
        session_log_sync(payload)

    assert str(exc_info.value) == message
