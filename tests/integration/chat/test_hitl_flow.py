from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import meridian.lib.chat.server as server
from meridian.lib.chat.protocol import ChatEvent, utc_now_iso
from meridian.lib.chat.server import app, configure
from meridian.lib.core.types import SpawnId


class Handle:
    def __init__(
        self,
        spawn_id: str = "p-hitl",
        *,
        pending_requests: set[str] | None = None,
        pending_inputs: set[str] | None = None,
    ) -> None:
        self.spawn_id = SpawnId(spawn_id)
        self.pending_requests = set() if pending_requests is None else set(pending_requests)
        self.pending_inputs = set() if pending_inputs is None else set(pending_inputs)
        self.messages: list[str] = []
        self.requests: list[tuple[str, str, dict[str, object] | None]] = []
        self.inputs: list[tuple[str, dict[str, object]]] = []
        self.cancel_calls = 0
        self.stop_calls = 0

    def health(self) -> bool:
        return True

    async def send_message(self, text: str) -> None:
        self.messages.append(text)

    async def send_cancel(self) -> None:
        self.cancel_calls += 1
        self.pending_requests.clear()
        self.pending_inputs.clear()

    async def stop(self) -> None:
        self.stop_calls += 1
        self.pending_requests.clear()
        self.pending_inputs.clear()

    async def respond_request(
        self,
        request_id: str,
        decision: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        if request_id not in self.pending_requests:
            raise ValueError(f"stale_hitl_request:{request_id}")
        self.pending_requests.remove(request_id)
        self.requests.append((request_id, decision, payload))

    async def respond_user_input(self, request_id: str, answers: dict[str, object]) -> None:
        if request_id not in self.pending_inputs:
            raise ValueError(f"stale_user_input:{request_id}")
        self.pending_inputs.remove(request_id)
        self.inputs.append((request_id, answers))


class Acquisition:
    def __init__(self, handles: list[Handle] | None = None) -> None:
        self._handles = iter(handles or [Handle()])
        self.calls: list[tuple[str, str, int]] = []

    async def acquire(
        self,
        chat_id: str,
        initial_prompt: str,
        *,
        execution_generation: int = 0,
        chat_state: str = "active",
    ) -> Handle:
        _ = chat_state
        self.calls.append((chat_id, initial_prompt, execution_generation))
        return next(self._handles)


def _create_active_chat(client: TestClient, *, prompt: str = "hi") -> str:
    chat_id = client.post("/chat", json={}).json()["chat_id"]
    assert client.post(f"/chat/{chat_id}/msg", json={"text": prompt}).json()["status"] == "accepted"
    return chat_id


def _ingest_event(
    client: TestClient,
    chat_id: str,
    event_type: str,
    *,
    request_id: str | None = None,
    payload: dict[str, object] | None = None,
) -> None:
    entry = server._runtime.live_entries[chat_id]
    handle = entry.session.current_execution
    assert handle is not None
    client.portal.call(
        entry.pipeline.ingest,
        ChatEvent(
            type=event_type,
            seq=0,
            chat_id=chat_id,
            execution_id=str(handle.spawn_id),
            timestamp=utc_now_iso(),
            request_id=request_id,
            payload={} if payload is None else payload,
        ),
    )
    client.portal.call(entry.pipeline.drain)


def _replayed_events(client: TestClient, chat_id: str) -> list[dict[str, object]]:
    stream_source = server._runtime.get_stream_source(chat_id)
    assert stream_source is not None
    expected_event_count = len(list(stream_source.event_log.read_all()))
    with client.websocket_connect(f"/ws/chat/{chat_id}") as ws:
        return [ws.receive_json() for _ in range(expected_event_count)]


def _drain_pipeline(client: TestClient, chat_id: str) -> None:
    entry = server._runtime.live_entries.get(chat_id)
    if entry is None:
        return
    client.portal.call(entry.pipeline.drain)


def test_approve_rest_emits_resolution_event_with_request_id(tmp_path: Path) -> None:
    handle = Handle(pending_requests={"r1"})
    configure(
        runtime_root=tmp_path,
        backend_acquisition=Acquisition([handle]),
        project_root=tmp_path,
    )

    with TestClient(app) as client:
        chat_id = _create_active_chat(client)
        _ingest_event(
            client,
            chat_id,
            "request.opened",
            request_id="r1",
            payload={"request_type": "approval", "command": "make test"},
        )

        approved = client.post(
            f"/chat/{chat_id}/approve",
            json={"request_id": "r1", "decision": "accept", "payload": {"x": 1}},
        ).json()
        _drain_pipeline(client, chat_id)
        events = _replayed_events(client, chat_id)

    assert approved == {"status": "accepted", "error": None}
    assert handle.requests == [("r1", "accept", {"x": 1})]
    relevant_events = [
        event
        for event in events
        if event["type"] in {"chat.started", "request.opened", "request.resolved"}
    ]
    assert [event["type"] for event in relevant_events] == [
        "chat.started",
        "request.opened",
        "request.resolved",
    ]
    assert relevant_events[-1]["request_id"] == "r1"
    assert relevant_events[-1]["payload"]["decision"] == "accept"


def test_answer_input_rest_forwards_text_response_to_backend_handle(tmp_path: Path) -> None:
    handle = Handle(pending_inputs={"i-text"})
    configure(
        runtime_root=tmp_path,
        backend_acquisition=Acquisition([handle]),
        project_root=tmp_path,
    )

    with TestClient(app) as client:
        chat_id = _create_active_chat(client)
        answered = client.post(
            f"/chat/{chat_id}/input",
            json={"request_id": "i-text", "answers": {"text": "Ada"}},
        ).json()

    assert answered == {"status": "accepted", "error": None}
    assert handle.inputs == [("i-text", {"text": "Ada"})]


def test_approve_for_stale_execution_generation_is_rejected_as_stale(tmp_path: Path) -> None:
    first = Handle(spawn_id="p-old", pending_requests={"r-stale"})
    second = Handle(spawn_id="p-new", pending_requests={"r-fresh"})
    acquisition = Acquisition([first, second])
    configure(runtime_root=tmp_path, backend_acquisition=acquisition, project_root=tmp_path)

    with TestClient(app) as client:
        chat_id = _create_active_chat(client, prompt="first")
        session = server._runtime.live_entries[chat_id].session
        session.on_execution_died(session.execution_generation)

        assert (
            client.post(f"/chat/{chat_id}/msg", json={"text": "second"}).json()["status"]
            == "accepted"
        )
        rejected = client.post(
            f"/chat/{chat_id}/approve",
            json={"request_id": "r-stale", "decision": "accept"},
        ).json()

    assert acquisition.calls == [(chat_id, "first", 1), (chat_id, "second", 2)]
    assert rejected["status"] == "rejected"
    assert "stale" in (rejected["error"] or "")
    assert second.requests == []


def test_multiple_pending_requests_each_emit_their_own_resolution(tmp_path: Path) -> None:
    handle = Handle(pending_requests={"r1", "r2"})
    configure(
        runtime_root=tmp_path,
        backend_acquisition=Acquisition([handle]),
        project_root=tmp_path,
    )

    with TestClient(app) as client:
        chat_id = _create_active_chat(client)
        _ingest_event(client, chat_id, "request.opened", request_id="r1")
        _ingest_event(client, chat_id, "request.opened", request_id="r2")

        assert (
            client.post(
                f"/chat/{chat_id}/approve",
                json={"request_id": "r1", "decision": "accept"},
            ).json()["status"]
            == "accepted"
        )
        assert (
            client.post(
                f"/chat/{chat_id}/approve",
                json={"request_id": "r2", "decision": "reject"},
            ).json()["status"]
            == "accepted"
        )
        _drain_pipeline(client, chat_id)
        events = _replayed_events(client, chat_id)

    resolved = [event for event in events if event["type"] == "request.resolved"]
    assert handle.requests == [("r1", "accept", None), ("r2", "reject", None)]
    assert [event["request_id"] for event in resolved] == ["r1", "r2"]
    assert [event["payload"]["decision"] for event in resolved] == ["accept", "reject"]


@pytest.mark.parametrize(
    ("endpoint", "body"),
    [
        ("approve", {"request_id": "r1", "decision": "accept"}),
        ("input", {"request_id": "i1", "answers": {"text": "hello"}}),
    ],
)
def test_hitl_commands_after_close_are_rejected_as_chat_closed(
    tmp_path: Path,
    endpoint: str,
    body: dict[str, object],
) -> None:
    handle = Handle(pending_requests={"r1"}, pending_inputs={"i1"})
    configure(
        runtime_root=tmp_path,
        backend_acquisition=Acquisition([handle]),
        project_root=tmp_path,
    )

    with TestClient(app) as client:
        chat_id = _create_active_chat(client)
        assert client.post(f"/chat/{chat_id}/close").json()["status"] == "accepted"
        rejected = client.post(f"/chat/{chat_id}/{endpoint}", json=body).json()

    assert rejected == {"status": "rejected", "error": "chat_closed"}


def test_cancel_during_pending_hitl_request_cleans_up_request(tmp_path: Path) -> None:
    handle = Handle(pending_requests={"r-cancel"}, pending_inputs={"i-cancel"})
    configure(
        runtime_root=tmp_path,
        backend_acquisition=Acquisition([handle]),
        project_root=tmp_path,
    )

    with TestClient(app) as client:
        chat_id = _create_active_chat(client)
        _ingest_event(client, chat_id, "request.opened", request_id="r-cancel")

        cancelled = client.post(f"/chat/{chat_id}/cancel").json()
        stale = client.post(
            f"/chat/{chat_id}/approve",
            json={"request_id": "r-cancel", "decision": "accept"},
        ).json()

    assert cancelled == {"status": "accepted", "error": None}
    assert handle.cancel_calls == 1
    assert handle.pending_requests == set()
    assert handle.pending_inputs == set()
    assert stale == {"status": "rejected", "error": "stale_hitl_request:r-cancel"}


def test_hitl_events_appear_in_replay_after_persistence(tmp_path: Path) -> None:
    handle = Handle(pending_requests={"r1"}, pending_inputs={"i1"})
    configure(
        runtime_root=tmp_path,
        backend_acquisition=Acquisition([handle]),
        project_root=tmp_path,
    )

    with TestClient(app) as client:
        chat_id = _create_active_chat(client)
        _ingest_event(
            client,
            chat_id,
            "request.opened",
            request_id="r1",
            payload={"request_type": "approval", "command": "make test"},
        )
        _ingest_event(
            client,
            chat_id,
            "user_input.requested",
            request_id="i1",
            payload={"request_type": "user_input", "questions": [{"id": "text"}]},
        )

        assert (
            client.post(
                f"/chat/{chat_id}/approve",
                json={"request_id": "r1", "decision": "accept"},
            ).json()["status"]
            == "accepted"
        )
        assert (
            client.post(
                f"/chat/{chat_id}/input",
                json={"request_id": "i1", "answers": {"text": "ship it"}},
            ).json()["status"]
            == "accepted"
        )
        _drain_pipeline(client, chat_id)
        assert client.post(f"/chat/{chat_id}/close").json()["status"] == "accepted"

    configure(runtime_root=tmp_path, backend_acquisition=Acquisition(), project_root=tmp_path)
    with TestClient(app) as client:
        events = _replayed_events(client, chat_id)

    relevant_events = [
        event
        for event in events
        if event["type"]
        in {
            "chat.started",
            "request.opened",
            "user_input.requested",
            "request.resolved",
            "user_input.resolved",
            "chat.exited",
        }
    ]
    assert [(event["type"], event.get("request_id")) for event in relevant_events] == [
        ("chat.started", None),
        ("request.opened", "r1"),
        ("user_input.requested", "i1"),
        ("request.resolved", "r1"),
        ("user_input.resolved", "i1"),
        ("chat.exited", None),
    ]
