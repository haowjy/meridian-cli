from pathlib import Path

import pytest

import meridian.lib.chat.server as chat_server
from meridian.lib.chat.policy import default_chat_policy_snapshot
from meridian.lib.chat.server import _UnconfiguredRuntime
from meridian.lib.service_context import ApplicationContext, ApplicationServices, ChatEntryPoint


def test_unconfigured_runtime_raises_for_any_access() -> None:
    runtime = _UnconfiguredRuntime()

    with pytest.raises(RuntimeError, match="not configured"):
        runtime.start()

    with pytest.raises(RuntimeError, match="not configured"):
        runtime.list_chats()


def test_configure_builds_runtime_from_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    runtime = object()
    entrypoint = ChatEntryPoint(
        context=ApplicationContext(project_root=Path("/project"), runtime_root=Path("/runtime")),
        services=ApplicationServices(),
    )

    def fake_builder(
        *,
        entrypoint,
        default_policy_snapshot,
        backend_acquisition=None,
        acquisition_factory=None,
    ):
        captured["entrypoint"] = entrypoint
        captured["default_policy_snapshot"] = default_policy_snapshot
        captured["backend_acquisition"] = backend_acquisition
        captured["acquisition_factory"] = acquisition_factory
        return runtime

    monkeypatch.setattr(chat_server, "build_chat_runtime_from_entrypoint", fake_builder)
    chat_server.configure(
        entrypoint=entrypoint,
        default_policy_snapshot=default_chat_policy_snapshot(),
    )

    assert captured["entrypoint"] is entrypoint
    assert captured["backend_acquisition"] is not None
    assert chat_server._configured_runtime() is runtime


def test_configure_builds_runtime_from_typed_request(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    runtime = object()

    def fake_builder(request):
        captured["request"] = request
        return runtime

    monkeypatch.setattr(chat_server, "build_chat_runtime", fake_builder)
    chat_server.configure(
        runtime_root=Path("/runtime"),
        project_root=Path("/project"),
    )

    request = captured["request"]
    assert request.runtime_root == Path("/runtime")
    assert request.project_root == Path("/project")
    assert chat_server._configured_runtime() is runtime
