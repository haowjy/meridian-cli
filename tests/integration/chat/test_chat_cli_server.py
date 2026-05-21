# qa-validated: test-suite-redesign
"""Chat CLI server startup and validation tests — port, host, harness, asset serving, ls/close."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest

from meridian.cli import chat_cmd
from meridian.cli.chat_cmd import run_chat_server
from meridian.lib.chat.policy import default_chat_policy_snapshot


@pytest.fixture(autouse=True)
def _stable_policy_resolution(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stub policy resolution for server tests — they don't exercise it.

    Excluded: test_chat_cli_rejects_unknown_harness, which must exercise real harness
    validation that lives inside _resolve_chat_policy_snapshot.
    """
    if request.node.name == "test_chat_cli_rejects_unknown_harness":
        return
    monkeypatch.setattr(
        chat_cmd,
        "_resolve_chat_policy_snapshot",
        lambda **_kwargs: default_chat_policy_snapshot(),
    )


def _write_dist(root: Path) -> Path:
    dist = root / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text("<html>chat ui</html>", encoding="utf-8")
    (assets / "index.js").write_text("console.log('chat')", encoding="utf-8")
    return dist


def test_chat_cli_auto_port_prints_local_backend_url(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: tmp_path / "runtime")
    calls: list[dict[str, object]] = []

    def fake_run(app, *, host: str, port: int) -> None:
        calls.append({"app": app, "host": host, "port": port})

    stdout = StringIO()
    actual_port = run_chat_server(
        host="127.0.0.1",
        port=0,
        harness="claude",
        headless=True,
        uvicorn_run=fake_run,
        stdout=stdout,
    )

    assert actual_port > 0
    assert stdout.getvalue() == f"Chat backend: http://127.0.0.1:{actual_port}\n"
    assert len(calls) == 1
    assert calls[0]["host"] == "127.0.0.1"
    assert calls[0]["port"] == actual_port


def test_chat_cli_uses_requested_host_and_port(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: tmp_path / "runtime")
    calls: list[tuple[str, int]] = []

    def fake_run(_app, *, host: str, port: int) -> None:
        calls.append((host, port))

    stdout = StringIO()
    actual_port = run_chat_server(
        host="0.0.0.0",
        port=8765,
        harness="codex",
        headless=True,
        uvicorn_run=fake_run,
        stdout=stdout,
    )

    assert actual_port == 8765
    assert calls == [("0.0.0.0", 8765)]
    assert stdout.getvalue() == "Chat backend: http://127.0.0.1:8765\n"


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_chat_cli_accepts_supported_harness_matrix(monkeypatch, tmp_path, harness: str) -> None:
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: tmp_path / harness)

    run_chat_server(
        harness=harness,
        port=8900,
        headless=True,
        uvicorn_run=lambda *_args, **_kwargs: None,
        stdout=StringIO(),
    )


def test_chat_cli_rejects_unknown_harness(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: tmp_path / "runtime")

    with pytest.raises(ValueError, match="unsupported chat harness"):
        run_chat_server(
            harness="bogus",
            port=8900,
            headless=True,
            uvicorn_run=lambda *_args, **_kwargs: None,
            stdout=StringIO(),
        )


def test_chat_cli_static_mode_mounts_assets_and_writes_server_discovery(
    monkeypatch, tmp_path
) -> None:
    from meridian.lib.service_context import ApplicationContext, ApplicationServices, ChatEntryPoint

    runtime_root = tmp_path / "runtime"
    dist = _write_dist(tmp_path)
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: runtime_root)
    mounted: dict[str, object] = {}

    def fake_mount(app, assets) -> None:
        mounted["app"] = app
        mounted["assets"] = assets

    monkeypatch.setattr("meridian.lib.chat.server.mount_frontend", fake_mount)
    stdout = StringIO()

    from typing import cast

    actual_port = run_chat_server(
        host="0.0.0.0",
        port=8765,
        frontend_dist=str(dist),
        entrypoint=ChatEntryPoint(
            context=ApplicationContext(project_root=tmp_path, runtime_root=runtime_root),
            services=ApplicationServices(),
        ),
        uvicorn_run=lambda *_args, **_kwargs: None,
        stdout=stdout,
    )

    assert actual_port == 8765
    assert stdout.getvalue().splitlines()[-1] == "Chat UI: http://127.0.0.1:8765"
    assert cast("object", mounted["assets"]).root == dist.resolve()
    discovery = runtime_root / "chat-server.json"
    assert discovery.exists()
    assert '"url": "http://127.0.0.1:8765"' in discovery.read_text(encoding="utf-8")


def test_chat_cli_static_mode_uses_default_asset_resolution(monkeypatch, tmp_path) -> None:
    from meridian.lib.service_context import ApplicationContext, ApplicationServices, ChatEntryPoint

    runtime_root = tmp_path / "runtime"
    dist = _write_dist(tmp_path)
    assets = chat_cmd.FrontendAssets(
        root=dist, index_html=dist / "index.html", assets_dir=dist / "assets"
    )
    mounted: dict[str, object] = {}

    def fake_mount(app, resolved_assets) -> None:
        mounted["app"] = app
        mounted["assets"] = resolved_assets

    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: runtime_root)
    monkeypatch.setattr(
        chat_cmd, "resolve_frontend_assets", lambda explicit_dist=None, **kwargs: assets
    )
    monkeypatch.setattr("meridian.lib.chat.server.mount_frontend", fake_mount)
    stdout = StringIO()

    actual_port = run_chat_server(
        port=8765,
        entrypoint=ChatEntryPoint(
            context=ApplicationContext(project_root=tmp_path, runtime_root=runtime_root),
            services=ApplicationServices(),
        ),
        uvicorn_run=lambda *_args, **_kwargs: None,
        stdout=stdout,
    )

    assert actual_port == 8765
    assert mounted["assets"] == assets
    assert stdout.getvalue() == "Chat UI: http://127.0.0.1:8765\n"
    discovery = runtime_root / "chat-server.json"
    assert discovery.exists()
    assert '"url": "http://127.0.0.1:8765"' in discovery.read_text(encoding="utf-8")


def test_chat_cli_headless_skips_frontend_serving(monkeypatch, tmp_path) -> None:
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: runtime_root)
    stdout = StringIO()

    actual_port = run_chat_server(
        host="127.0.0.1",
        port=8765,
        headless=True,
        uvicorn_run=lambda *_args, **_kwargs: None,
        stdout=stdout,
    )

    assert actual_port == 8765
    assert stdout.getvalue() == "Chat backend: http://127.0.0.1:8765\n"


def test_chat_cli_missing_assets_exits_with_actionable_error(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: tmp_path / "runtime")
    stdout = StringIO()

    with pytest.raises(SystemExit) as exc_info:
        run_chat_server(
            port=8765,
            frontend_dist=str(tmp_path / "missing"),
            uvicorn_run=lambda *_args, **_kwargs: None,
            stdout=stdout,
        )

    assert exc_info.value.code == 1
    output = stdout.getvalue()
    assert "Built frontend assets not found" in output
    assert "meridian chat --frontend-dist /path/to/dist" in output
    assert "meridian chat --headless" in output


def test_chat_cli_default_static_mode_falls_back_to_headless_when_assets_do_not_resolve(
    monkeypatch, tmp_path
) -> None:
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: runtime_root)
    monkeypatch.setattr(
        chat_cmd, "resolve_frontend_assets", lambda explicit_dist=None, **kwargs: None
    )
    stdout = StringIO()
    calls: list[tuple[str, int]] = []

    def fake_run(_app, *, host: str, port: int) -> None:
        calls.append((host, port))

    actual_port = run_chat_server(port=8765, uvicorn_run=fake_run, stdout=stdout)

    assert actual_port == 8765
    assert calls == [("127.0.0.1", 8765)]
    assert stdout.getvalue() == (
        "Note: Frontend assets not found. Running in headless mode.\n"
        "To serve the UI, build assets first: cd ../meridian-web && pnpm build\n"
        "Chat backend: http://127.0.0.1:8765\n"
    )


def test_chat_ls_uses_discovered_server_url(monkeypatch, tmp_path, capsys) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    (runtime_root / "chat-server.json").write_text(
        '{"url":"http://127.0.0.1:9999"}\n', encoding="utf-8"
    )
    from meridian.lib.service_context import ApplicationContext, ApplicationServices, ChatEntryPoint

    monkeypatch.setattr(
        chat_cmd,
        "_prepare_chat_runtime_read_entrypoint",
        lambda: ChatEntryPoint(
            context=ApplicationContext(project_root=project_root, runtime_root=runtime_root),
            services=ApplicationServices(),
        ),
    )

    def fake_request(method, path, *, timeout):
        assert method == "GET"
        assert path == "http://127.0.0.1:9999/chat"
        assert timeout == 5.0

        class Response:
            status_code = 200
            text = ""

            def json(self):
                return {
                    "chats": [
                        {"chat_id": "c-1", "state": "idle", "created_at": "2026-04-30T00:00:00Z"}
                    ]
                }

        return Response()

    monkeypatch.setattr("httpx.request", fake_request)

    chat_cmd._chat_ls()

    output = capsys.readouterr().out
    assert "chat_id" in output
    assert "c-1" in output
    assert "idle" in output


def test_chat_close_uses_runtime_scoped_discovery_over_global_discovery(
    monkeypatch, tmp_path, capsys
) -> None:
    from meridian.lib.service_context import ApplicationContext, ApplicationServices, ChatEntryPoint

    project_root = tmp_path / "project-b"
    project_root.mkdir()
    runtime_root = tmp_path / "runtime-b"
    runtime_root.mkdir()
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    (runtime_root / "chat-server.json").write_text(
        '{"url":"http://127.0.0.1:2222"}\n', encoding="utf-8"
    )
    (user_home / "chat-server.json").write_text(
        '{"url":"http://127.0.0.1:1111"}\n', encoding="utf-8"
    )
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: user_home)
    monkeypatch.setattr(
        chat_cmd,
        "_prepare_chat_runtime_read_entrypoint",
        lambda: ChatEntryPoint(
            context=ApplicationContext(project_root=project_root, runtime_root=runtime_root),
            services=ApplicationServices(),
        ),
    )

    def fake_request(method, path, *, timeout):
        assert method == "POST"
        assert path == "http://127.0.0.1:2222/chat/c1/close"
        assert timeout == 5.0

        class Response:
            status_code = 200
            text = ""

            def json(self):
                return {"status": "accepted"}

        return Response()

    monkeypatch.setattr("httpx.request", fake_request)

    chat_cmd._chat_close("c1")

    assert capsys.readouterr().out == "closed c1\n"
