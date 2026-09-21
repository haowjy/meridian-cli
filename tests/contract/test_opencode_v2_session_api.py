"""OpenCode V2 session API request-shape contract coverage.

The V1 counterpart lives in ``test_opencode_session_api.py``. V2 resumes over the
``/api`` session surface and applies an explicit model with
``POST /api/session/{id}/model`` rather than dropping it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

from meridian.lib.harness.connections.base import HarnessRequest
from meridian.lib.harness.connections.opencode_http import _permission_liveness_key
from meridian.lib.harness.connections.opencode_v2_http import OpenCodeV2Connection
from meridian.lib.harness.projections.projection_errors import HarnessCapabilityMismatch
from meridian.lib.launch.launch_types import ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver


class _TestableOpenCodeV2Connection(OpenCodeV2Connection):
    def __init__(
        self,
        responses: list[tuple[int, object | None, str] | Exception] | None = None,
        *,
        get_responses: list[tuple[int, object | None, str] | Exception] | None = None,
    ) -> None:
        super().__init__()
        self.requests: list[tuple[str, str, dict[str, object]]] = []
        self._responses = iter(responses or [])
        self._get_responses = iter(get_responses or [])

    async def _post_json(
        self,
        path: str,
        payload: Mapping[str, object],
        *,
        skip_body_on_statuses: frozenset[int] | None = None,
        tolerate_incomplete_body: bool = False,
    ) -> tuple[int, object | None, str]:
        _ = skip_body_on_statuses, tolerate_incomplete_body
        self.requests.append(("POST", path, dict(payload)))
        try:
            response = next(self._responses)
        except StopIteration as exc:
            raise AssertionError("Unexpected _post_json call in test") from exc
        if isinstance(response, Exception):
            raise response
        return response

    async def _get_json(self, path: str) -> tuple[int, object | None, str]:
        self.requests.append(("GET", path, {}))
        try:
            response = next(self._get_responses)
        except StopIteration as exc:
            raise AssertionError("Unexpected _get_json call in test") from exc
        if isinstance(response, Exception):
            raise response
        return response


def _spec(
    *, model: str | None = None, continue_session_id: str | None = None
) -> ResolvedLaunchSpec:
    return ResolvedLaunchSpec(
        prompt="hello",
        model=model,
        continue_session_id=continue_session_id,
        permission_resolver=UnsafeNoOpPermissionResolver(_suppress_warning=True),
    )


@pytest.mark.asyncio
async def test_v2_resume_applies_explicit_model_via_model_endpoint() -> None:
    connection = _TestableOpenCodeV2Connection(
        responses=[(204, None, "")],
        get_responses=[(200, {"data": {"id": "ses_v2"}}, "")],
    )

    session_id = await connection._create_session(
        _spec(model="openai/gpt-5.5", continue_session_id="ses_v2")
    )

    assert session_id == "ses_v2"
    assert connection.requests == [
        ("GET", "/api/session/ses_v2", {}),
        (
            "POST",
            "/api/session/ses_v2/model",
            {"model": {"providerID": "openai", "id": "gpt-5.5"}},
        ),
    ]


@pytest.mark.asyncio
async def test_v2_resume_without_model_does_not_switch() -> None:
    connection = _TestableOpenCodeV2Connection(
        get_responses=[(200, {"data": {"id": "ses_v2"}}, "")],
    )

    session_id = await connection._create_session(_spec(continue_session_id="ses_v2"))

    assert session_id == "ses_v2"
    assert connection.requests == [("GET", "/api/session/ses_v2", {})]


@pytest.mark.asyncio
async def test_v2_resume_model_switch_fails_loudly_on_html_fallback() -> None:
    connection = _TestableOpenCodeV2Connection(
        responses=[(200, "<html></html>", "text/html")],
        get_responses=[(200, {"data": {"id": "ses_v2"}}, "")],
    )

    with pytest.raises(HarnessCapabilityMismatch, match="model switch failed"):
        await connection._create_session(
            _spec(model="openai/gpt-5.5", continue_session_id="ses_v2")
        )


@pytest.mark.asyncio
async def test_respond_request_v2_posts_reply_to_permission_endpoint() -> None:
    connection = _TestableOpenCodeV2Connection(responses=[(200, None, "")])
    connection._pending_requests["per_1"] = "ses_v2"

    await connection.respond_request("per_1", "reject", {"message": "not allowed"})

    assert connection.requests == [
        (
            "POST",
            "/api/session/ses_v2/permission/per_1/reply",
            {"reply": "reject", "message": "not allowed"},
        ),
    ]
    assert connection._pending_requests == {}


@pytest.mark.asyncio
async def test_respond_request_v2_fails_loudly_on_html_fallback() -> None:
    connection = _TestableOpenCodeV2Connection(responses=[(200, "<html></html>", "text/html")])
    connection._session_id = "ses_v2"
    # The reply now requires a known pending request id (unknown ids fail fast),
    # so register it here to keep exercising the HTML-fallback guard.
    connection._pending_requests["per_1"] = "ses_v2"

    with pytest.raises(RuntimeError, match="V2 permission reply failed"):
        await connection.respond_request("per_1", "accept")


class _RecordingPermissionHandler:
    no_runtime_hitl = False

    def __init__(self) -> None:
        self.resolutions: list[tuple[str, dict[str, object] | None]] = []

    async def handle_request(
        self,
        connection: OpenCodeV2Connection,
        request: HarnessRequest,
    ) -> None:
        _ = connection, request

    async def on_request_resolved(
        self,
        request_id: str,
        *,
        resolution: dict[str, object] | None = None,
    ) -> None:
        self.resolutions.append((request_id, resolution))


@pytest.mark.asyncio
async def test_v2_permission_reply_event_resolves_pending_request() -> None:
    connection = _TestableOpenCodeV2Connection()
    handler = _RecordingPermissionHandler()
    connection._request_handler = handler
    connection._session_id = "ses_v2"
    connection._pending_requests["per_1"] = "ses_v2"
    connection._liveness.signal_request_in_flight(_permission_liveness_key("per_1"))

    event = connection._event_from_json_line(
        json.dumps(
            {
                "type": "permission.v2.replied",
                "data": {
                    "properties": {
                        "sessionID": "ses_v2",
                        "requestID": "per_1",
                        "reply": "always",
                    }
                },
            }
        ),
        raw_text="permission.v2.replied",
    )
    assert event is not None

    consumed = await connection._dispatch_inbound_event(event)

    assert consumed is True
    assert connection._pending_requests == {}
    assert _permission_liveness_key("per_1") not in connection._liveness._active_requests
    assert handler.resolutions == [("per_1", {"decision": "accept", "reply": "always"})]

