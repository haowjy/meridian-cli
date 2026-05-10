from __future__ import annotations

import importlib
import os
from io import StringIO
from pathlib import Path
from typing import cast

import pytest

from meridian.cli import chat_cmd
from meridian.cli.chat_cmd import run_chat_server
from meridian.cli.output import OutputConfig
from meridian.lib.catalog.catalog_session import CatalogSession
from meridian.lib.catalog.model_aliases import AliasEntry
from meridian.lib.chat.policy import build_chat_backend_launch_plan, default_chat_policy_snapshot
from meridian.lib.core.types import HarnessId, ModelId, SpawnId
from meridian.lib.harness.registry import get_default_harness_registry
from meridian.lib.launch.launch_types import CompositionWarning, ResolvedLaunchSpec
from meridian.lib.safety.permissions import UnsafeNoOpPermissionResolver
from meridian.lib.service_context import ApplicationContext, ApplicationServices, ChatEntryPoint
from tests.support.fixtures import write_skill

cli_main = importlib.import_module("meridian.cli.main")


class EmptyPipelineLookup:
    def __init__(self, snapshot=None) -> None:
        self._snapshot = snapshot or default_chat_policy_snapshot()

    def get_pipeline(self, chat_id: str):
        _ = chat_id
        return None

    def get_policy_snapshot(self, chat_id: str):
        _ = chat_id
        return self._snapshot


class _UnexpectedCall(RuntimeError):
    pass


class _FakeLauncher:
    pass


@pytest.fixture(autouse=True)
def _stable_policy_resolution(monkeypatch, request):
    if request.node.name.startswith("test_chat_policy_snapshot_") or request.node.name in {
        "test_chat_cli_rejects_unknown_harness",
    }:
        return
    monkeypatch.setattr(
        chat_cmd,
        "_resolve_chat_policy_snapshot",
        lambda **_kwargs: default_chat_policy_snapshot(),
    )


def test_chat_cli_auto_port_prints_local_backend_url(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: tmp_path / "runtime")
    monkeypatch.chdir(tmp_path)
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
    monkeypatch.chdir(tmp_path)
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


@pytest.mark.parametrize("harness", ["claude", "codex", "opencode"])
def test_chat_cli_accepts_supported_harness_matrix(monkeypatch, tmp_path, harness: str) -> None:
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: tmp_path / harness)
    monkeypatch.chdir(tmp_path)

    run_chat_server(
        harness=harness,
        port=8900,
        headless=True,
        uvicorn_run=lambda *_args, **_kwargs: None,
        stdout=StringIO(),
    )


def test_chat_cli_rejects_unknown_harness(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: tmp_path / "runtime")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="unsupported chat harness"):
        run_chat_server(
            harness="bogus",
            port=8900,
            headless=True,
            uvicorn_run=lambda *_args, **_kwargs: None,
            stdout=StringIO(),
        )


def _write_dist(root: Path) -> Path:
    dist = root / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text("<html>chat ui</html>", encoding="utf-8")
    (assets / "index.js").write_text("console.log('chat')", encoding="utf-8")
    return dist


def _write_dev_frontend(root: Path) -> Path:
    frontend_root = root / "meridian-web"
    (frontend_root / "node_modules").mkdir(parents=True)
    (frontend_root / "package.json").write_text('{"scripts":{"dev":"vite"}}', encoding="utf-8")
    return frontend_root


def test_chat_cli_static_mode_mounts_assets_and_writes_server_discovery(
    monkeypatch, tmp_path
) -> None:
    runtime_root = tmp_path / "runtime"
    dist = _write_dist(tmp_path)
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: runtime_root)
    monkeypatch.chdir(tmp_path)
    mounted: dict[str, object] = {}

    def fake_mount(app, assets) -> None:
        mounted["app"] = app
        mounted["assets"] = assets

    monkeypatch.setattr("meridian.lib.chat.server.mount_frontend", fake_mount)
    stdout = StringIO()

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
    monkeypatch.chdir(tmp_path)
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
    monkeypatch.chdir(tmp_path)
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


def test_chat_command_falls_back_to_globally_parsed_harness(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_chat_server(**kwargs) -> int:
        captured.update(kwargs)
        return 8765

    monkeypatch.setattr(chat_cmd, "run_chat_server", fake_run_chat_server)
    token = cli_main._GLOBAL_OPTIONS.set(
        cli_main.GlobalOptions(output=OutputConfig(format="text"), harness="codex")
    )
    try:
        chat_cmd._chat(port=8765)
    finally:
        cli_main._GLOBAL_OPTIONS.reset(token)

    assert captured["harness"] == "codex"
    assert captured["headless"] is False
    assert captured["frontend_dist"] is None
    assert captured["open_browser"] is False
    assert captured["autocompact"] is None


def test_chat_command_prefers_explicit_harness_over_global_default(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_chat_server(**kwargs) -> int:
        captured.update(kwargs)
        return 8765

    monkeypatch.setattr(chat_cmd, "run_chat_server", fake_run_chat_server)
    token = cli_main._GLOBAL_OPTIONS.set(
        cli_main.GlobalOptions(output=OutputConfig(format="text"), harness="claude")
    )
    try:
        chat_cmd._chat(
            harness="opencode",
            port=8765,
            headless=True,
            frontend_dist="/tmp/dist",
            open_browser=True,
        )
    finally:
        cli_main._GLOBAL_OPTIONS.reset(token)

    assert captured["harness"] == "opencode"
    assert captured["headless"] is True
    assert captured["frontend_dist"] == "/tmp/dist"
    assert captured["open_browser"] is True


def test_chat_command_passes_autocompact_to_chat_server(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_chat_server(**kwargs) -> int:
        captured.update(kwargs)
        return 8765

    monkeypatch.setattr(chat_cmd, "run_chat_server", fake_run_chat_server)
    chat_cmd._chat(port=8765, autocompact=55)

    assert captured["autocompact"] == 55


@pytest.mark.parametrize("harness", [HarnessId.CLAUDE, HarnessId.CODEX, HarnessId.OPENCODE])
def test_backend_acquisition_preserves_requested_harness(tmp_path, harness: HarnessId) -> None:
    snapshot = default_chat_policy_snapshot(harness=harness, model="model-x")
    acquisition = chat_cmd._build_backend_acquisition(
        runtime_root=tmp_path / "runtime",
        project_root=tmp_path,
        pipeline_lookup=EmptyPipelineLookup(snapshot),
    )

    plan = acquisition._build_launch_plan("c1", "hello")
    config = plan.connection_config
    spec = plan.spec

    assert config.harness_id == harness
    assert spec.model == "model-x"


@pytest.mark.parametrize(
    ("harness_name", "expected_harness"),
    [
        ("claude", HarnessId.CLAUDE),
        ("codex", HarnessId.CODEX),
        ("opencode", HarnessId.OPENCODE),
    ],
)
def test_chat_cli_builds_runtime_with_factory_inputs(
    monkeypatch, tmp_path, harness_name: str, expected_harness: HarnessId
) -> None:
    captured: dict[str, object] = {}
    snapshot = default_chat_policy_snapshot(harness=expected_harness, model="model-x")
    runtime = object()
    entrypoint = ChatEntryPoint(
        context=ApplicationContext(
            project_root=tmp_path,
            runtime_root=tmp_path / "runtime",
        ),
        services=ApplicationServices(),
    )

    def fake_build_chat_runtime_from_entrypoint(
        *,
        entrypoint,
        default_policy_snapshot,
        backend_acquisition=None,
        acquisition_factory=None,
    ):
        _ = backend_acquisition
        captured["entrypoint"] = entrypoint
        captured["default_policy_snapshot"] = default_policy_snapshot
        captured["acquisition_factory"] = acquisition_factory
        return runtime

    def fake_configure(*, runtime) -> None:
        captured["configured_runtime"] = runtime

    monkeypatch.setattr(
        chat_cmd,
        "build_chat_runtime_from_entrypoint",
        fake_build_chat_runtime_from_entrypoint,
    )
    monkeypatch.setattr(chat_cmd, "_resolve_chat_policy_snapshot", lambda **_kwargs: snapshot)

    import meridian.lib.chat.server as chat_server

    monkeypatch.setattr(chat_server, "configure", fake_configure)
    monkeypatch.setattr(chat_server, "app", object())

    run_chat_server(
        harness=harness_name,
        model="model-x",
        port=8900,
        headless=True,
        uvicorn_run=lambda *_args, **_kwargs: None,
        stdout=StringIO(),
        entrypoint=entrypoint,
    )

    assert captured["entrypoint"] is entrypoint
    assert captured["default_policy_snapshot"] == snapshot
    assert captured["configured_runtime"] is runtime

    factory = cast("chat_cmd._ChatBackendAcquisitionFactory", captured["acquisition_factory"])
    assert factory.policy_snapshot == snapshot

    lookup = EmptyPipelineLookup(snapshot)
    acquisition = factory.build(
        pipeline_lookup=lookup,
        project_root=tmp_path,
        runtime_root=cast("Path", entrypoint.context.runtime_root),
    )
    plan = acquisition._build_launch_plan("c1", "hello")
    config = plan.connection_config
    spec = plan.spec

    assert config.harness_id == expected_harness
    assert config.project_root == tmp_path
    assert spec.model == "model-x"


def test_chat_cli_blocks_nested_launch(monkeypatch, tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime"
    configured: list[object] = []
    monkeypatch.setenv("MERIDIAN_DEPTH", "1")
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: runtime_root)

    import meridian.lib.chat.server as chat_server

    monkeypatch.setattr(chat_server, "configure", lambda **kwargs: configured.append(kwargs))
    monkeypatch.setattr(chat_server, "app", object())

    with pytest.raises(
        ValueError,
        match="blocked in nested/delegated Meridian execution",
    ):
        run_chat_server(
            port=8765,
            headless=True,
            uvicorn_run=lambda *_args, **_kwargs: None,
            stdout=StringIO(),
        )

    assert configured == []
    assert not (runtime_root / "chat-server.json").exists()


def test_stale_asset_warning_mentions_rebuild(monkeypatch, tmp_path) -> None:
    project = tmp_path / "meridian-cli"
    source_dir = tmp_path / "meridian-web" / "src"
    source_dir.mkdir(parents=True)
    dist = _write_dist(tmp_path)
    index = dist / "index.html"
    source_file = source_dir / "App.tsx"
    source_file.write_text("export function App() { return null }", encoding="utf-8")
    newer = index.stat().st_mtime + 10
    os.utime(source_file, (newer, newer))
    monkeypatch.setattr(chat_cmd, "require_established_project_root", lambda: project)

    stdout = StringIO()
    chat_cmd._check_stale_assets(
        chat_cmd.FrontendAssets(root=dist, index_html=index, assets_dir=dist / "assets"),
        stdout,
    )

    assert "Frontend source is newer than built assets" in stdout.getvalue()
    assert "pnpm build" in stdout.getvalue()


def test_chat_command_meridian_env_dev_enables_dev_mode(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_chat_server(**kwargs) -> int:
        captured.update(kwargs)
        return 8765

    monkeypatch.setenv("MERIDIAN_ENV", "dev")
    monkeypatch.setattr(chat_cmd, "run_chat_server", fake_run_chat_server)
    token = cli_main._GLOBAL_OPTIONS.set(
        cli_main.GlobalOptions(output=OutputConfig(format="text"), harness="claude")
    )
    try:
        chat_cmd._chat(port=8765)
    finally:
        cli_main._GLOBAL_OPTIONS.reset(token)

    assert captured["dev"] is True
    assert captured["frontend_root"] is None


def test_chat_command_headless_takes_precedence_over_meridian_env_dev(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run_chat_server(**kwargs) -> int:
        captured.update(kwargs)
        return 8765

    monkeypatch.setenv("MERIDIAN_ENV", "dev")
    monkeypatch.setattr(chat_cmd, "run_chat_server", fake_run_chat_server)
    token = cli_main._GLOBAL_OPTIONS.set(
        cli_main.GlobalOptions(output=OutputConfig(format="text"), harness="claude")
    )
    try:
        chat_cmd._chat(port=8765, headless=True)
    finally:
        cli_main._GLOBAL_OPTIONS.reset(token)

    assert captured["headless"] is True
    assert captured["dev"] is False


def test_chat_policy_resolution_fails_before_runtime_configure_or_discovery_write(
    monkeypatch, tmp_path
) -> None:
    runtime_root = tmp_path / "runtime"
    configured: list[object] = []
    write_bootstrap_calls: list[Path] = []
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: runtime_root)
    monkeypatch.setattr(chat_cmd, "require_established_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        chat_cmd,
        "prepare_for_runtime_write",
        lambda root: write_bootstrap_calls.append(root),
    )
    monkeypatch.setattr(
        chat_cmd,
        "_resolve_chat_policy_snapshot",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("policy-failed")),
    )

    import meridian.lib.chat.server as chat_server

    monkeypatch.setattr(chat_server, "configure", lambda **kwargs: configured.append(kwargs))
    monkeypatch.setattr(chat_server, "app", object())

    with pytest.raises(ValueError, match="policy-failed"):
        run_chat_server(
            port=8765,
            headless=True,
            uvicorn_run=lambda *_args, **_kwargs: None,
            stdout=StringIO(),
        )

    assert configured == []
    assert write_bootstrap_calls == []
    assert not (runtime_root / "chat-server.json").exists()


def _mock_alias(alias: str, model_id: str, harness: HarnessId) -> AliasEntry:
    return AliasEntry(alias=alias, model_id=ModelId(model_id), resolved_harness=harness)


def test_chat_policy_snapshot_resolves_alias_to_canonical_model(monkeypatch, tmp_path) -> None:
    alias_entry = _mock_alias("codex", "gpt-5.3-codex", HarnessId.CODEX)

    def fake_resolve_model(self: CatalogSession, token: str) -> AliasEntry:
        _ = self
        if token in {"codex", "gpt-5.3-codex"}:
            return alias_entry
        raise ValueError(f"unknown model {token}")

    monkeypatch.setattr(CatalogSession, "resolve_model", fake_resolve_model)
    monkeypatch.setattr(CatalogSession, "load_aliases", lambda self: [alias_entry])

    snapshot = chat_cmd._resolve_chat_policy_snapshot(
        project_root=tmp_path,
        model="codex",
        harness="codex",
        agent=None,
        skills=(),
        approval=None,
        sandbox=None,
        effort=None,
        autocompact=None,
    )

    assert snapshot.canonical_model_id == "gpt-5.3-codex"
    assert snapshot.harness == "codex"


def test_chat_policy_snapshot_rejects_incompatible_explicit_harness(monkeypatch, tmp_path) -> None:
    alias_entry = _mock_alias("gptmini", "gpt-5.4-mini", HarnessId.CODEX)

    def fake_resolve_model(self: CatalogSession, token: str) -> AliasEntry:
        _ = self
        if token in {"gptmini", "gpt-5.4-mini"}:
            return alias_entry
        raise ValueError(f"unknown model {token}")

    monkeypatch.setattr(CatalogSession, "resolve_model", fake_resolve_model)
    monkeypatch.setattr(CatalogSession, "load_aliases", lambda self: [alias_entry])

    with pytest.raises(ValueError, match="incompatible"):
        chat_cmd._resolve_chat_policy_snapshot(
            project_root=tmp_path,
            model="gptmini",
            harness="claude",
            agent=None,
            skills=(),
            approval=None,
            sandbox=None,
            effort=None,
            autocompact=None,
        )


def test_chat_policy_snapshot_without_model_does_not_force_catalog_lookup(
    monkeypatch, tmp_path
) -> None:
    calls: list[str] = []

    def fail_resolve_model(self: CatalogSession, token: str) -> AliasEntry:
        _ = self
        calls.append(f"resolve:{token}")
        raise AssertionError("catalog resolve_model should not run without a model token")

    def fail_load_aliases(self: CatalogSession) -> list[AliasEntry]:
        _ = self
        calls.append("load_aliases")
        raise AssertionError("catalog load_aliases should not run without a model token")

    monkeypatch.setattr(CatalogSession, "resolve_model", fail_resolve_model)
    monkeypatch.setattr(CatalogSession, "load_aliases", fail_load_aliases)

    snapshot = chat_cmd._resolve_chat_policy_snapshot(
        project_root=tmp_path,
        model=None,
        harness=None,
        agent=None,
        skills=(),
        approval=None,
        sandbox=None,
        effort=None,
        autocompact=None,
    )

    assert calls == []
    assert snapshot.harness == "claude"
    assert snapshot.canonical_model_id == ""


def test_chat_policy_snapshot_collects_profile_and_missing_skill_warnings(tmp_path) -> None:
    (tmp_path / "meridian.toml").write_text(
        '[primary]\nagent = "missing-default"\n',
        encoding="utf-8",
    )

    snapshot = chat_cmd._resolve_chat_policy_snapshot(
        project_root=tmp_path,
        model=None,
        harness=None,
        agent=None,
        skills=("missing-skill",),
        approval=None,
        sandbox=None,
        effort=None,
        autocompact=None,
    )

    warning_codes = [warning.code for warning in snapshot.warnings]
    assert "profile_warning" in warning_codes
    assert "missing_skills_warning" in warning_codes
    warning_messages = [warning.message for warning in snapshot.warnings]
    assert any(
        "Configured agent profile 'missing-default' is unavailable" in message
        for message in warning_messages
    )
    assert any("Warning: Skipped unavailable skills: missing-skill" in m for m in warning_messages)


def test_chat_policy_snapshot_explicit_missing_agent_fails_before_startup(
    monkeypatch, tmp_path
) -> None:
    runtime_root = tmp_path / "runtime"
    configured: list[object] = []
    write_bootstrap_calls: list[Path] = []
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: runtime_root)
    monkeypatch.setattr(chat_cmd, "require_established_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        chat_cmd,
        "prepare_for_runtime_write",
        lambda root: write_bootstrap_calls.append(root),
    )

    import meridian.lib.chat.server as chat_server

    monkeypatch.setattr(chat_server, "configure", lambda **kwargs: configured.append(kwargs))
    monkeypatch.setattr(chat_server, "app", object())

    with pytest.raises(FileNotFoundError, match="missing-explicit-agent"):
        run_chat_server(
            agent="missing-explicit-agent",
            port=8765,
            headless=True,
            uvicorn_run=lambda *_args, **_kwargs: None,
            stdout=StringIO(),
        )

    assert configured == []
    assert write_bootstrap_calls == []
    assert not (runtime_root / "chat-server.json").exists()


def test_chat_policy_snapshot_loads_harness_and_model_skill_variant(monkeypatch, tmp_path) -> None:
    alias_entry = _mock_alias("gptmini", "gpt-5.4-mini", HarnessId.CODEX)
    write_skill(tmp_path, "variant-skill", body="Base body")
    token_variant = (
        tmp_path
        / ".mars"
        / "skills"
        / "variant-skill"
        / "variants"
        / "codex"
        / "gptmini"
        / "SKILL.md"
    )
    token_variant.parent.mkdir(parents=True, exist_ok=True)
    token_variant.write_text("Token variant body\n", encoding="utf-8")
    canonical_variant = (
        tmp_path
        / ".mars"
        / "skills"
        / "variant-skill"
        / "variants"
        / "codex"
        / "gpt-5.4-mini"
        / "SKILL.md"
    )
    canonical_variant.parent.mkdir(parents=True, exist_ok=True)
    canonical_variant.write_text("Canonical variant body\n", encoding="utf-8")

    def fake_resolve_model(self: CatalogSession, token: str) -> AliasEntry:
        _ = self
        if token in {"gptmini", "gpt-5.4-mini"}:
            return alias_entry
        raise ValueError(f"unknown model {token}")

    monkeypatch.setattr(CatalogSession, "resolve_model", fake_resolve_model)
    monkeypatch.setattr(CatalogSession, "load_aliases", lambda self: [alias_entry])

    snapshot = chat_cmd._resolve_chat_policy_snapshot(
        project_root=tmp_path,
        model="gptmini",
        harness="codex",
        agent=None,
        skills=("variant-skill",),
        approval=None,
        sandbox=None,
        effort=None,
        autocompact=None,
    )

    assert snapshot.skills == ("variant-skill",)
    assert [doc.logical_name for doc in snapshot.prompt_inputs.skill_documents] == ["variant-skill"]
    assert "Token variant body" in snapshot.prompt_inputs.skill_documents[0].content
    assert "Base body" not in snapshot.prompt_inputs.skill_documents[0].content
    assert snapshot.prompt_inputs.skill_documents[0].path == token_variant.resolve().as_posix()


def test_chat_policy_snapshot_approval_env_beats_profile_and_config(
    monkeypatch, tmp_path
) -> None:
    alias_entry = _mock_alias("gptmini", "gpt-5.4-mini", HarnessId.CODEX)
    monkeypatch.setenv("MERIDIAN_APPROVAL", "confirm")
    (tmp_path / "meridian.toml").write_text(
        '[primary]\napproval = "auto"\n',
        encoding="utf-8",
    )
    (tmp_path / "mars.toml").write_text('[settings]\ntargets = [".claude"]\n', encoding="utf-8")
    (tmp_path / ".mars" / "agents").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".mars" / "agents" / "reviewer.md").write_text(
        "---\n"
        "name: reviewer\n"
        "approval: yolo\n"
        "---\n\n"
        "Reviewer profile body.\n",
        encoding="utf-8",
    )

    def fake_resolve_model(self: CatalogSession, token: str) -> AliasEntry:
        _ = self
        if token in {"gptmini", "gpt-5.4-mini"}:
            return alias_entry
        raise ValueError(f"unknown model {token}")

    monkeypatch.setattr(CatalogSession, "resolve_model", fake_resolve_model)
    monkeypatch.setattr(CatalogSession, "load_aliases", lambda self: [alias_entry])

    snapshot = chat_cmd._resolve_chat_policy_snapshot(
        project_root=tmp_path,
        model="gptmini",
        harness=None,
        agent="reviewer",
        skills=(),
        approval=None,
        sandbox=None,
        effort=None,
        autocompact=None,
    )

    assert snapshot.execution_policy.approval == "confirm"
    assert snapshot.field_provenance["approval"] == "env"


def test_chat_policy_snapshot_with_agent_and_cli_overrides_feeds_launch_plan(
    monkeypatch, tmp_path
) -> None:
    alias_entry = _mock_alias("gptmini", "gpt-5.4-mini", HarnessId.CODEX)
    write_skill(tmp_path, "profile-skill", body="Profile skill body")
    write_skill(tmp_path, "cli-skill", body="CLI skill body")
    (tmp_path / "mars.toml").write_text('[settings]\ntargets = [".claude"]\n', encoding="utf-8")
    (tmp_path / ".mars" / "agents").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".mars" / "agents" / "reviewer.md").write_text(
        "---\n"
        "name: reviewer\n"
        "model: claude-sonnet-4-6\n"
        "approval: auto\n"
        "autocompact: 200000\n"
        "sandbox: workspace-write\n"
        "skills:\n"
        "  - profile-skill\n"
        "tools:\n"
        "  - Read\n"
        "disallowed-tools:\n"
        "  - Bash\n"
        "mcp-tools:\n"
        "  - github=gh\n"
        "model-policies:\n"
        "  - match:\n"
        "      model: gpt-5.4-mini\n"
        "    override:\n"
        "      effort: low\n"
        "---\n\n"
        "Reviewer profile body.\n",
        encoding="utf-8",
    )

    def fake_resolve_model(self: CatalogSession, token: str) -> AliasEntry:
        _ = self
        if token in {"gptmini", "gpt-5.4-mini"}:
            return alias_entry
        raise ValueError(f"unknown model {token}")

    monkeypatch.setattr(CatalogSession, "resolve_model", fake_resolve_model)
    monkeypatch.setattr(CatalogSession, "load_aliases", lambda self: [alias_entry])

    snapshot = chat_cmd._resolve_chat_policy_snapshot(
        project_root=tmp_path,
        model="gptmini",
        harness=None,
        agent="reviewer",
        skills=("cli-skill", "profile-skill"),
        approval=None,
        sandbox=None,
        effort=None,
        autocompact=None,
    )

    assert snapshot.requested_model_token == "gptmini"
    assert snapshot.selected_model_token == "gptmini"
    assert snapshot.canonical_model_id == "gpt-5.4-mini"
    assert snapshot.harness == "codex"
    assert snapshot.agent_name == "reviewer"
    assert snapshot.execution_policy.approval == "auto"
    assert snapshot.execution_policy.sandbox == "workspace-write"
    assert snapshot.execution_policy.effort == "low"
    assert snapshot.execution_policy.autocompact == 200000
    assert snapshot.skills == ("profile-skill", "cli-skill")
    assert snapshot.allowed_tools == ("Read",)
    assert snapshot.disallowed_tools == ("Bash",)
    assert snapshot.mcp_tools == ("github=gh",)
    assert snapshot.prompt_inputs.agent_profile_body == ""
    assert snapshot.prompt_inputs.adhoc_agent_payload == "Reviewer profile body."
    assert [doc.logical_name for doc in snapshot.prompt_inputs.skill_documents] == [
        "profile-skill",
        "cli-skill",
    ]

    adapter = get_default_harness_registry().get_subprocess_harness(HarnessId.CODEX)
    plan = build_chat_backend_launch_plan(
        snapshot=snapshot,
        initial_prompt="Please review this change.",
        spawn_id=SpawnId("chat-test"),
        adapter=adapter,
        project_root=tmp_path,
        runtime_root=tmp_path / "runtime",
    )

    assert plan.harness_id == HarnessId.CODEX
    assert plan.connection_config.harness_id == HarnessId.CODEX
    assert isinstance(plan.spec, ResolvedLaunchSpec)
    assert plan.spec.model == "gpt-5.4-mini"
    assert plan.spec.base_instructions == "Reviewer profile body."
    assert plan.spec.user_turn_content == "Please review this change."
    assert "Profile skill body" in (plan.spec.developer_instructions or "")
    assert "CLI skill body" in (plan.spec.developer_instructions or "")
    assert plan.spec.mcp_tools == ("github=gh",)
    assert not isinstance(plan.spec.permission_resolver, UnsafeNoOpPermissionResolver)
    assert "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE" not in plan.connection_config.env_overrides
    assert plan.configured_payload == {
        "harness": "codex",
        "model": "gpt-5.4-mini",
        "requested_model_token": "gptmini",
        "selected_model_token": "gptmini",
        "harness_provenance": "mars-provided",
        "policy_snapshot_id": snapshot.snapshot_id,
    }


def test_chat_cli_prints_policy_warnings_before_backend_url(monkeypatch, tmp_path) -> None:
    runtime_root = tmp_path / "runtime"
    snapshot = default_chat_policy_snapshot().model_copy(
        update={
            "warnings": (
                CompositionWarning(code="profile_warning", message="profile warning"),
                CompositionWarning(code="missing_skills_warning", message="missing skill warning"),
            )
        }
    )
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: runtime_root)
    monkeypatch.setattr(chat_cmd, "_resolve_chat_policy_snapshot", lambda **_kwargs: snapshot)
    stdout = StringIO()

    run_chat_server(
        port=8765,
        headless=True,
        uvicorn_run=lambda *_args, **_kwargs: None,
        stdout=stdout,
    )

    assert stdout.getvalue() == (
        "Warning (profile_warning): profile warning\n"
        "Warning (missing_skills_warning): missing skill warning\n"
        "Chat backend: http://127.0.0.1:8765\n"
    )


@pytest.mark.parametrize(
    ("argv", "flag"),
    [
        (["chat", "ls", "--model", "codex"], "--model"),
        (["chat", "show", "c1", "--approval", "auto"], "--approval"),
        (["chat", "log", "c1", "--skills", "md-validation"], "--skills"),
        (["chat", "close", "c1", "--agent", "reviewer"], "--agent"),
    ],
)
def test_chat_management_subcommands_reject_launch_policy_flags(
    argv: list[str], flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(argv)

    assert exc_info.value.code == 1
    stderr = capsys.readouterr().err
    assert 'Unknown option: "' in stderr
    assert flag in stderr or (
        flag == "--agent" and '"-a"' in stderr
    )


@pytest.mark.parametrize(
    ("argv", "selector"),
    [
        (["--harness", "codex", "chat", "ls"], "--harness"),
        (["codex", "chat", "ls"], "codex"),
    ],
)
def test_chat_management_subcommands_reject_global_harness_selectors(
    argv: list[str], selector: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(argv)

    message = str(exc_info.value)
    assert "Unknown option" in message
    assert selector in message
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("kwargs", "expected_error"),
    [
        (
            {"dev": True, "frontend_dist": "/tmp/dist"},
            "Error: --frontend-dist cannot be combined with --dev.\n",
        ),
        (
            {"frontend_root": "/tmp/frontend"},
            "Error: --frontend-root is only valid with --dev.\n",
        ),
        ({"no_portless": True}, "Error: --no-portless is only valid with --dev.\n"),
        ({"tailscale": True}, "Error: --tailscale and --funnel are only valid with --dev.\n"),
        ({"funnel": True}, "Error: --tailscale and --funnel are only valid with --dev.\n"),
        (
            {"portless_force": True},
            "Error: --portless-force is only valid with portless dev mode.\n",
        ),
    ],
)
def test_chat_cli_rejects_invalid_flag_combinations_before_startup(
    monkeypatch, tmp_path, kwargs: dict[str, object], expected_error: str
) -> None:
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: runtime_root)
    monkeypatch.chdir(tmp_path)
    stdout = StringIO()
    launch_attempts: list[str] = []

    def forbid(*_args, **_kwargs):
        launch_attempts.append("called")
        raise _UnexpectedCall("startup collaborator should not run")

    monkeypatch.setattr(chat_cmd, "resolve_frontend_assets", forbid)

    with pytest.raises(SystemExit) as exc_info:
        run_chat_server(
            port=8765,
            uvicorn_run=forbid,
            stdout=stdout,
            **kwargs,
        )

    assert exc_info.value.code == 1
    assert stdout.getvalue() == expected_error
    assert launch_attempts == []


def test_chat_cli_headless_rejects_dev_mode(monkeypatch, tmp_path) -> None:
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: runtime_root)
    monkeypatch.chdir(tmp_path)
    stdout = StringIO()

    with pytest.raises(SystemExit) as exc_info:
        run_chat_server(
            port=8765,
            headless=True,
            dev=True,
            uvicorn_run=lambda *_args, **_kwargs: None,
            stdout=stdout,
        )

    assert exc_info.value.code == 1
    assert stdout.getvalue() == "Error: --dev cannot be combined with --headless.\n"


@pytest.mark.parametrize(
    ("kwargs", "expected_error"),
    [
        (
            {"headless": True, "no_portless": True},
            "Error: --no-portless is only valid with --dev.\n",
        ),
        (
            {"headless": True, "tailscale": True},
            "Error: --tailscale and --funnel are only valid with --dev.\n",
        ),
        (
            {"headless": True, "funnel": True},
            "Error: --tailscale and --funnel are only valid with --dev.\n",
        ),
        (
            {"headless": True, "portless_force": True},
            "Error: dev frontend flags cannot be combined with --headless.\n",
        ),
    ],
)
def test_chat_cli_headless_rejects_dev_frontend_flags(
    monkeypatch, tmp_path, kwargs, expected_error: str
) -> None:
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: runtime_root)
    stdout = StringIO()

    with pytest.raises(SystemExit) as exc_info:
        run_chat_server(port=8765, stdout=stdout, **kwargs)

    assert exc_info.value.code == 1
    assert stdout.getvalue() == expected_error


def test_chat_cli_dev_mode_reports_missing_frontend_checkout_actionably(
    monkeypatch, tmp_path
) -> None:
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: runtime_root)
    monkeypatch.setattr(
        "meridian.lib.chat.dev_frontend.resolve_dev_frontend_root",
        lambda *, explicit=None, **kwargs: None,
    )
    stdout = StringIO()

    with pytest.raises(SystemExit) as exc_info:
        run_chat_server(port=8765, dev=True, stdout=stdout)

    assert exc_info.value.code == 1
    output = stdout.getvalue()
    assert "Error: Dev frontend checkout not found." in output
    assert "--frontend-root /path/to/meridian-web" in output
    assert "MERIDIAN_DEV_FRONTEND_ROOT=/path/to/meridian-web" in output
    assert "meridian chat --headless" in output


def test_chat_cli_dev_mode_reports_prerequisite_failures(monkeypatch, tmp_path) -> None:
    runtime_root = tmp_path / "runtime"
    frontend_root = tmp_path / "meridian-web"
    frontend_root.mkdir()
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: runtime_root)
    stdout = StringIO()

    with pytest.raises(SystemExit) as exc_info:
        run_chat_server(
            port=8765,
            dev=True,
            frontend_root=str(frontend_root),
            stdout=stdout,
        )

    assert exc_info.value.code == 1
    expected = f"Error: Frontend root is missing package.json: {frontend_root.resolve()}\n"
    assert stdout.getvalue() == expected


def test_chat_cli_dev_mode_surfaces_launcher_configuration_errors(monkeypatch, tmp_path) -> None:
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: runtime_root)
    stdout = StringIO()

    def raise_config_error(**_kwargs):
        from meridian.lib.chat.dev_frontend import DevFrontendConfigurationError

        raise DevFrontendConfigurationError("--tailscale/--funnel require portless")

    monkeypatch.setattr(
        "meridian.lib.chat.dev_frontend.resolve_dev_frontend_launcher",
        raise_config_error,
    )

    with pytest.raises(SystemExit) as exc_info:
        run_chat_server(port=8765, dev=True, stdout=stdout)

    assert exc_info.value.code == 1
    assert stdout.getvalue() == "Error: --tailscale/--funnel require portless\n"


def test_chat_cli_dev_mode_uses_frontend_root_launcher_supervisor_and_warning(
    monkeypatch, tmp_path
) -> None:
    runtime_root = tmp_path / "runtime"
    frontend_root = _write_dev_frontend(tmp_path)
    launcher = _FakeLauncher()
    captured: dict[str, object] = {}

    class FakeSupervisor:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        async def run(self) -> int:
            captured["ran"] = True
            return 0

    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: runtime_root)
    monkeypatch.setattr(
        "meridian.lib.chat.dev_frontend.resolve_dev_frontend_launcher",
        lambda **_kwargs: launcher,
    )
    monkeypatch.setattr("meridian.lib.chat.dev_frontend.DevSupervisor", FakeSupervisor)
    monkeypatch.chdir(tmp_path)
    stdout = StringIO()

    actual_port = run_chat_server(
        host="0.0.0.0",
        port=8765,
        dev=True,
        frontend_root=str(frontend_root),
        open_browser=True,
        entrypoint=ChatEntryPoint(
            context=ApplicationContext(project_root=tmp_path, runtime_root=runtime_root),
            services=ApplicationServices(),
        ),
        stdout=stdout,
    )

    assert actual_port == 8765
    assert captured["backend_host"] == "0.0.0.0"
    assert captured["backend_port"] == 8765
    assert captured["frontend_root"] == frontend_root.resolve()
    assert captured["open_browser"] is True
    assert captured["launcher"] is launcher
    assert captured["ran"] is True
    assert stdout.getvalue() == (
        "Warning: Backend is bound to all interfaces."
        " The frontend sharing mode does not restrict backend API access.\n"
    )
    discovery = runtime_root / "chat-server.json"
    assert discovery.exists()
    assert '"url": "http://127.0.0.1:8765"' in discovery.read_text(encoding="utf-8")


def test_chat_cli_dev_mode_surfaces_frontend_launch_errors(monkeypatch, tmp_path) -> None:
    runtime_root = tmp_path / "runtime"
    frontend_root = _write_dev_frontend(tmp_path)

    class FakeSupervisor:
        def __init__(self, **_kwargs) -> None:
            pass

        async def run(self) -> int:
            from meridian.lib.chat.dev_frontend.launcher import FrontendLaunchError

            raise FrontendLaunchError("portless failed to start")

    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: runtime_root)
    monkeypatch.setattr("meridian.lib.chat.dev_frontend.DevSupervisor", FakeSupervisor)
    stdout = StringIO()

    with pytest.raises(SystemExit) as exc_info:
        run_chat_server(
            port=8765,
            dev=True,
            frontend_root=str(frontend_root),
            stdout=stdout,
        )

    assert exc_info.value.code == 1
    assert stdout.getvalue() == "Error: portless failed to start\n"


def test_chat_cli_headless_warns_on_open(monkeypatch, tmp_path) -> None:
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr("meridian.cli.chat_cmd.get_user_home", lambda: runtime_root)
    monkeypatch.chdir(tmp_path)
    stdout = StringIO()
    opened: list[str] = []
    monkeypatch.setattr(chat_cmd.webbrowser, "open", lambda url: opened.append(url))

    actual_port = run_chat_server(
        port=8765,
        headless=True,
        open_browser=True,
        uvicorn_run=lambda *_args, **_kwargs: None,
        stdout=stdout,
    )

    assert actual_port == 8765
    assert opened == []
    assert stdout.getvalue() == (
        "Warning: --open is ignored in headless mode.\n"
        "Chat backend: http://127.0.0.1:8765\n"
    )
