"""FastAPI transport surface for the headless chat backend."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from meridian.lib.chat.commands import ChatCommand, CommandResult
from meridian.lib.chat.frontend import FrontendAssets
from meridian.lib.chat.protocol import utc_now_iso
from meridian.lib.chat.replay import ReplayService
from meridian.lib.chat.runtime import ChatRuntime
from meridian.lib.state.user_paths import get_user_home
from meridian.lib.telemetry import emit_telemetry
from meridian.lib.telemetry.events import make_error_data

if TYPE_CHECKING:
    from meridian.lib.chat.backend_acquisition import BackendAcquisition


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    runtime = _configured_runtime()
    await runtime.start()
    try:
        yield
    finally:
        await runtime.stop()


app = FastAPI(lifespan=lifespan)


class CreateChatRequest(BaseModel):
    """Create-chat transport shape."""

    model_config = ConfigDict(extra="forbid")


class CreateChatResponse(BaseModel):
    chat_id: str
    state: str


class PromptRequest(BaseModel):
    text: str


class ApprovalRequest(BaseModel):
    request_id: str
    decision: str
    payload: dict[str, object] | None = None


class InputRequest(BaseModel):
    request_id: str
    answers: dict[str, object]


class RevertRequest(BaseModel):
    commit_sha: str


class StateResponse(BaseModel):
    chat_id: str
    state: str


class ChatListItemResponse(BaseModel):
    chat_id: str
    state: str
    created_at: str | None = None


class ChatListResponse(BaseModel):
    chats: list[ChatListItemResponse]


class ChatEventsResponse(BaseModel):
    chat_id: str
    events: list[dict[str, object]]


class _UnavailableAcquisition:
    async def acquire(
        self,
        chat_id: str,
        initial_prompt: str,
        *,
        execution_generation: int = 0,
        chat_state: str = "active",
    ):
        _ = (chat_id, initial_prompt, execution_generation, chat_state)
        raise RuntimeError("chat backend acquisition is not configured")


class _UnconfiguredRuntime:
    """Sentinel that fails loudly if the chat server is used before configure()."""

    def __getattr__(self, name: str) -> object:
        raise RuntimeError(
            f"Chat server runtime is not configured. "
            f"Call configure() before accessing '{name}'."
        )


_runtime: ChatRuntime | _UnconfiguredRuntime = _UnconfiguredRuntime()


def _configured_runtime() -> ChatRuntime:
    return cast("ChatRuntime", _runtime)


def configure(
    *,
    runtime: ChatRuntime | None = None,
    runtime_root: Path | None = None,
    backend_acquisition: BackendAcquisition | None = None,
    project_root: Path | None = None,
) -> None:
    """Configure process-local chat server dependencies."""

    global _runtime
    if runtime is not None:
        _runtime = runtime
        return
    _runtime = ChatRuntime(
        runtime_root=runtime_root or get_user_home(),
        project_root=project_root or Path.cwd(),
        backend_acquisition=backend_acquisition or _UnavailableAcquisition(),
    )


@app.get("/chat", response_model=ChatListResponse)
async def list_chats() -> ChatListResponse:
    start = time.monotonic()
    response = ChatListResponse(
        chats=[
            ChatListItemResponse(
                chat_id=item.chat_id,
                state=item.state,
                created_at=item.created_at,
            )
            for item in _configured_runtime().list_chats()
        ]
    )
    _emit_http_request_completed(
        method="GET",
        path="/chat",
        status_code=200,
        start=start,
        data={"result_count": len(response.chats)},
    )
    return response


@app.post("/chat", response_model=CreateChatResponse)
async def create_chat(body: CreateChatRequest) -> CreateChatResponse:
    start = time.monotonic()
    _ = body
    view = await _configured_runtime().create_chat()
    _emit_http_request_completed(
        method="POST",
        path="/chat",
        chat_id=view.chat_id,
        status_code=200,
        start=start,
    )
    return CreateChatResponse(chat_id=view.chat_id, state=view.state)


@app.post("/chat/{chat_id}/msg", response_model=CommandResult)
async def prompt_chat(chat_id: str, body: PromptRequest) -> CommandResult:
    return await _dispatch_rest(
        chat_id, "prompt", {"text": body.text}, method="POST", path=f"/chat/{chat_id}/msg"
    )


@app.post("/chat/{chat_id}/cancel", response_model=CommandResult)
async def cancel_chat(chat_id: str) -> CommandResult:
    return await _dispatch_rest(
        chat_id, "cancel", {}, method="POST", path=f"/chat/{chat_id}/cancel"
    )


@app.post("/chat/{chat_id}/approve", response_model=CommandResult)
async def approve_request(chat_id: str, body: ApprovalRequest) -> CommandResult:
    payload: dict[str, Any] = {"request_id": body.request_id, "decision": body.decision}
    if body.payload is not None:
        payload["payload"] = body.payload
    return await _dispatch_rest(
        chat_id, "approve", payload, method="POST", path=f"/chat/{chat_id}/approve"
    )


@app.post("/chat/{chat_id}/input", response_model=CommandResult)
async def answer_input(chat_id: str, body: InputRequest) -> CommandResult:
    return await _dispatch_rest(
        chat_id,
        "answer_input",
        {"request_id": body.request_id, "answers": body.answers},
        method="POST",
        path=f"/chat/{chat_id}/input",
    )


@app.post("/chat/{chat_id}/revert", response_model=CommandResult)
async def revert_checkpoint(chat_id: str, body: RevertRequest) -> CommandResult:
    return await _dispatch_rest(
        chat_id,
        "revert",
        {"commit_sha": body.commit_sha},
        method="POST",
        path=f"/chat/{chat_id}/revert",
    )


@app.post("/chat/{chat_id}/close", response_model=CommandResult)
async def close_chat(chat_id: str) -> CommandResult:
    return await _dispatch_rest(chat_id, "close", {}, method="POST", path=f"/chat/{chat_id}/close")


@app.get("/chat/{chat_id}/events", response_model=ChatEventsResponse)
async def get_chat_events(
    chat_id: str,
    last: int | None = Query(default=None, ge=0),
) -> ChatEventsResponse:
    start = time.monotonic()
    path = f"/chat/{chat_id}/events"
    events = _configured_runtime().list_events(chat_id, last=last)
    if events is None:
        _emit_http_request_completed(
            method="GET",
            path=path,
            chat_id=chat_id,
            status_code=404,
            start=start,
            data={"error": "chat_not_found", "last": last},
        )
        raise HTTPException(status_code=404, detail="chat_not_found")
    _emit_http_request_completed(
        method="GET",
        path=path,
        chat_id=chat_id,
        status_code=200,
        start=start,
        data={"event_count": len(events), "last": last},
    )
    return ChatEventsResponse(chat_id=chat_id, events=[asdict(event) for event in events])


@app.get("/chat/{chat_id}/state", response_model=StateResponse)
async def get_chat_state(chat_id: str) -> StateResponse:
    start = time.monotonic()
    path = f"/chat/{chat_id}/state"
    state = _configured_runtime().get_state(chat_id)
    if state is not None:
        _emit_http_request_completed(
            method="GET",
            path=path,
            chat_id=chat_id,
            status_code=200,
            start=start,
            data={"state": state},
        )
        return StateResponse(chat_id=chat_id, state=state)
    _emit_http_request_completed(
        method="GET",
        path=path,
        chat_id=chat_id,
        status_code=404,
        start=start,
        data={"error": "chat_not_found"},
    )
    raise HTTPException(status_code=404, detail="chat_not_found")


@app.websocket("/ws/chat/{chat_id}")
async def ws_chat(websocket: WebSocket, chat_id: str) -> None:
    await websocket.accept()
    last_seq = _parse_last_seq(websocket)
    stream_source = _configured_runtime().get_stream_source(chat_id)
    if stream_source is None:
        error_data = make_error_data(message="chat_not_found")
        error_data["error"]["type"] = "WebSocketRejected"
        _emit_ws_event(
            "chat.ws.rejected",
            chat_id=chat_id,
            last_seq=last_seq,
            data={**error_data, "close_code": 4004},
            severity="warning",
        )
        await websocket.close(code=4004, reason="chat_not_found")
        return
    _emit_ws_event("chat.ws.connected", chat_id=chat_id, last_seq=last_seq)
    send_lock = asyncio.Lock()
    replay = ReplayService(stream_source.event_log, stream_source.fanout, send_lock)
    close_code: int | None = None

    async def outbound() -> None:
        await replay.connect(websocket, last_seq)

    async def inbound() -> None:
        nonlocal close_code
        while True:
            try:
                raw_obj = await websocket.receive_json()
            except WebSocketDisconnect as exc:
                close_code = exc.code
                return
            except ValueError:
                close_code = 1003
                await websocket.close(code=1003, reason="invalid_json")
                return
            if not isinstance(raw_obj, dict) or "command_type" not in raw_obj:
                continue
            raw_mapping = cast("dict[object, object]", raw_obj)
            raw: dict[str, object] = {str(key): value for key, value in raw_mapping.items()}
            ack = await _handle_ws_command(raw, chat_id)
            async with send_lock:
                await websocket.send_json(ack)

    try:
        outbound_task = asyncio.create_task(outbound())
        inbound_task = asyncio.create_task(inbound())
        done, pending = await asyncio.wait(
            {outbound_task, inbound_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            try:
                task.result()
            except WebSocketDisconnect as exc:
                close_code = exc.code
        for task in pending:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
    finally:
        _emit_ws_event(
            "chat.ws.disconnected",
            chat_id=chat_id,
            last_seq=last_seq,
            data={"close_code": close_code},
        )


async def _dispatch_rest(
    chat_id: str,
    command_type: str,
    payload: dict[str, Any],
    *,
    method: str,
    path: str,
) -> CommandResult:
    start = time.monotonic()
    command = ChatCommand(
        type=command_type,
        command_id=str(uuid4()),
        chat_id=chat_id,
        timestamp=utc_now_iso(),
        payload=payload,
    )
    result = await _configured_runtime().dispatch(command)
    _emit_http_request_completed(
        method=method,
        path=path,
        chat_id=chat_id,
        command_id=command.command_id,
        status_code=200,
        start=start,
        data={
            "command_type": command_type,
            "result_status": result.status,
            **({"error": result.error} if result.error is not None else {}),
        },
    )
    return result


def _emit_http_request_completed(
    *,
    method: str,
    path: str,
    status_code: int,
    start: float,
    chat_id: str | None = None,
    command_id: str | None = None,
    data: dict[str, object] | None = None,
) -> None:
    ids: dict[str, str] = {}
    if chat_id:
        ids["chat_id"] = chat_id
    if command_id:
        ids["command_id"] = command_id
    emit_telemetry(
        "chat",
        "chat.http.request_completed",
        scope="chat.server",
        ids=ids or None,
        data={
            "method": method,
            "path": path,
            "status_code": status_code,
            "latency_ms": round((time.monotonic() - start) * 1000, 1),
            **(data or {}),
        },
    )


def _emit_ws_event(
    event: str,
    *,
    chat_id: str,
    last_seq: int | None,
    data: dict[str, object] | None = None,
    severity: str = "info",
) -> None:
    emit_telemetry(
        "chat",
        event,
        scope="chat.server.ws",
        ids={"chat_id": chat_id},
        data={"last_seq": last_seq, **(data or {})},
        severity=severity,
    )


async def _handle_ws_command(raw: dict[str, object], path_chat_id: str) -> dict[str, str]:
    command_id = str(raw.get("command_id") or "unknown")
    try:
        command = _parse_chat_command(raw, path_chat_id)
    except ValueError as exc:
        return {"ack": command_id, "status": "rejected", "error": str(exc)}
    result = await _configured_runtime().dispatch(command)
    payload = {"ack": command.command_id, "status": result.status}
    if result.error is not None:
        payload["error"] = result.error
    return payload


def _parse_chat_command(raw: dict[str, object], path_chat_id: str) -> ChatCommand:
    command_type = raw.get("command_type")
    command_id = raw.get("command_id")
    chat_id = raw.get("chat_id", path_chat_id)
    timestamp = raw.get("timestamp", utc_now_iso())
    payload = raw.get("payload", {})
    if not isinstance(command_type, str) or not command_type:
        raise ValueError("invalid_command:missing_command_type")
    if not isinstance(command_id, str) or not command_id:
        raise ValueError("invalid_command:missing_command_id")
    if chat_id != path_chat_id or not isinstance(chat_id, str):
        raise ValueError("invalid_command:chat_id_mismatch")
    if not isinstance(timestamp, str):
        raise ValueError("invalid_command:invalid_timestamp")
    if not isinstance(payload, dict):
        raise ValueError("invalid_command:payload_not_object")
    return ChatCommand(
        type=command_type,
        command_id=command_id,
        chat_id=chat_id,
        timestamp=timestamp,
        payload={str(key): value for key, value in cast("dict[object, object]", payload).items()},
    )


def mount_frontend(application: FastAPI, assets: FrontendAssets) -> None:
    """Mount SPA static serving on an existing FastAPI app.

    Must be called after all API and WebSocket routes are registered so those
    routes retain priority over the catch-all SPA fallback.
    """

    application.router.routes = [
        route
        for route in application.router.routes
        if getattr(route, "name", None) not in {"frontend-assets", "spa_fallback"}
    ]

    if assets.assets_dir.is_dir():
        application.mount(
            "/assets",
            StaticFiles(directory=str(assets.assets_dir)),
            name="frontend-assets",
        )

    @application.get("/{path:path}")
    async def spa_fallback(path: str) -> FileResponse:
        _ = path
        return FileResponse(str(assets.index_html), media_type="text/html")

    _ = spa_fallback


def _parse_last_seq(websocket: WebSocket) -> int | None:
    raw = websocket.query_params.get("last_seq")
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


__all__ = ["app", "configure", "mount_frontend"]
