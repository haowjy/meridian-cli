"""Only owned Codex identity envelopes establish a native key."""

import pytest

from meridian.lib.core.types import HarnessId
from meridian.lib.harness.bundle import get_harness_bundle
from meridian.lib.harness.connections.base import RawHarnessEvent

SID = "12345678-1234-4234-8234-123456789abc"


@pytest.mark.parametrize(
    "payload, expected",
    [
        (
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": f"codex resume {SID}"},
            },
            None,
        ),
        ({"type": "item.completed", "item": {"session_id": SID}}, None),
        ({"type": "thread.started", "thread_id": SID}, SID),
        ({"type": "session_id", "session_id": SID}, SID),
    ],
)
def test_owned_envelope_only(payload: dict[str, object], expected: str | None) -> None:
    from meridian.lib.harness.bundle import get_harness_bundle

    extractor = get_harness_bundle(HarnessId.CODEX).extractor
    fold = extractor.create_fold()
    facts = fold.facts
    _fold(fold, payload)
    assert facts.first_session_id == expected
    event = RawHarnessEvent(event_type=str(payload["type"]), harness_id="codex", payload=payload)
    assert (
        get_harness_bundle(HarnessId.CODEX).extractor.detect_session_id_from_event(event)
        == expected
    )


def test_claude_ignores_identity_keys_inside_message_content() -> None:
    extractor = get_harness_bundle(HarnessId.CLAUDE).extractor
    fold = extractor.create_fold()
    facts = fold.facts
    _fold(fold, {"type": "assistant", "message": {"session_id": SID}})
    assert facts.first_session_id is None
    _fold(fold, {"type": "system", "session_id": SID})
    assert facts.first_session_id == SID


def _fold(fold, payload):
    fold(
        RawHarnessEvent(
            harness_id="fixture",
            event_type=payload.get("type", payload.get("event_type", "")),
            payload=payload,
        )
    )
