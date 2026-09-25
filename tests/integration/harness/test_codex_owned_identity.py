"""Only owned Codex identity envelopes establish a native key."""

import json

import pytest

from meridian.lib.core.types import ArtifactKey, HarnessId, SpawnId
from meridian.lib.harness.connections.base import RawHarnessEvent
from meridian.lib.harness.registry import HarnessRegistry
from meridian.lib.state.artifact_store import InMemoryStore

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

    adapter = HarnessRegistry.with_defaults().get(HarnessId.CODEX)
    artifacts = InMemoryStore()
    artifacts.put(ArtifactKey("p1/output.jsonl"), (json.dumps(payload) + "\n").encode())
    assert adapter.extract_session_id(artifacts, SpawnId("p1")) == expected
    event = RawHarnessEvent(event_type=str(payload["type"]), harness_id="codex", payload=payload)
    assert (
        get_harness_bundle(HarnessId.CODEX).extractor.detect_session_id_from_event(event)
        == expected
    )


def test_claude_ignores_identity_keys_inside_message_content() -> None:
    adapter = HarnessRegistry.with_defaults().get(HarnessId.CLAUDE)
    artifacts = InMemoryStore()
    payload = {"type": "assistant", "message": {"session_id": SID}}
    artifacts.put(ArtifactKey("p1/output.jsonl"), (json.dumps(payload) + "\n").encode())
    assert adapter.extract_session_id(artifacts, SpawnId("p1")) is None
    payload = {"type": "system", "session_id": SID}
    artifacts.put(ArtifactKey("p1/output.jsonl"), (json.dumps(payload) + "\n").encode())
    assert adapter.extract_session_id(artifacts, SpawnId("p1")) == SID
