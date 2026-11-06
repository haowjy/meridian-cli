"""Artifact service logic and patched CLI workflow."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from pathlib import Path

import pytest

from meridian.cli.app_tree import artifact_app
from meridian.cli.artifact_cmd import (
    cmd_artifact_gc,  # noqa: F401 -- imports register commands on artifact_app
)
from meridian.lib.artifact import service, store
from meridian.lib.artifact.store import Serve


def test_teardown_is_surgically_scoped_to_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def fake_tailscale(args: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service, "_tailscale", fake_tailscale)
    assert service.tailscale_off("my-report", 50000, funnel=False)

    assert commands == [["serve", "--https=50000", "--set-path=/my-report", "off"]]


class _FakeProcess:
    pid = 4242

    def poll(self) -> None:
        return None


def _serve(
    slug: str,
    port: int,
    *,
    created: str = "2026-01-01T00:00:00Z",
    ttl: int | None = 60,
    pid: int = 4242,
    process_created_at: float | None = 1000.0,
) -> Serve:
    return Serve(slug, port, "/tmp", None, created, ttl, False, pid, process_created_at)


@pytest.mark.integration
def test_prefix_static_server_serves_tailscale_forwarded_paths(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("artifact home", encoding="utf-8")
    subdirectory = tmp_path / "sub"
    subdirectory.mkdir()
    (subdirectory / "file.txt").write_text("nested artifact", encoding="utf-8")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "meridian.lib.artifact._serve_dir",
            str(tmp_path),
            str(port),
            "demo",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def request(path: str) -> tuple[int, str | None, bytes]:
        connection = HTTPConnection("127.0.0.1", port, timeout=1)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.getheader("Location"), response.read()
        finally:
            connection.close()

    try:
        deadline = time.monotonic() + 2
        while True:
            try:
                status, _location, body = request("/demo/")
                break
            except OSError:
                if time.monotonic() >= deadline:
                    pytest.fail("prefix static server did not become ready")
                time.sleep(0.02)

        assert status == 200
        assert body == b"artifact home"
        assert request("/demo/sub/file.txt") == (200, None, b"nested artifact")
        redirect_status, location, _body = request("/demo")
        assert redirect_status == 301
        assert location == "/demo/"
        assert request("/other/")[0] == 404
    finally:
        proc.terminate()
        proc.wait(timeout=2)


@pytest.mark.parametrize("slug", ["", "/", "..", "a/b", " white ", "a--b", "x\nroot"])
def test_invalid_persisted_slugs_are_dropped_without_tailscale_calls(
    slug: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "serves.json"
    state_path.write_text(
        json.dumps(
            [
                {
                    "slug": slug,
                    "local_port": 50000,
                    "dir": "/tmp",
                    "work_id": None,
                    "created": "2020-01-01T00:00:00Z",
                    "ttl_seconds": 1,
                    "funnel": False,
                    "pid": 123,
                }
            ]
        ),
        encoding="utf-8",
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(store, "serves_path", lambda: state_path)
    monkeypatch.setattr(
        service,
        "_tailscale",
        lambda args: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )

    assert service.sweep_expired()[0] == []
    assert not service.stop_serve(slug)
    assert calls == []
    assert json.loads(state_path.read_text(encoding="utf-8")) == []


def test_tailscale_boundaries_reject_invalid_slug_without_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service, "_tailscale", lambda _args: pytest.fail("tailscale must not run"))
    with pytest.raises(service.ArtifactError):
        service._tailscale_register(50000, "", funnel=False)
    with pytest.raises(service.ArtifactError):
        service.tailscale_off("/", 50000, funnel=False)


def test_stop_failure_preserves_entry_and_pid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_path = tmp_path / "serves.json"
    monkeypatch.setattr(store, "serves_path", lambda: state_path)
    store.save_serves([_serve("report", 50000)])
    monkeypatch.setattr(
        service,
        "_tailscale",
        lambda args: subprocess.CompletedProcess(args, 1, "", "tailscale offline"),
    )
    monkeypatch.setattr(
        service, "terminate_pid", lambda *_args: pytest.fail("pid must remain alive")
    )

    with pytest.raises(SystemExit) as exit_info:
        artifact_app(["stop", "report"])

    assert exit_info.value.code == 1
    assert [item.slug for item in store.load_serves()] == ["report"]
    assert "tailscale offline" in capsys.readouterr().err


def test_failed_post_registration_rollback_persists_recovery_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "serves.json"
    monkeypatch.setattr(store, "serves_path", lambda: state_path)
    monkeypatch.setattr(service, "pick_port", lambda _occupied: 50000)
    monkeypatch.setattr(service, "tailscale_serve_ports", lambda: set())
    monkeypatch.setattr(service, "launch_http_server", lambda *_args: _FakeProcess())
    monkeypatch.setattr(service, "wait_for_http_server", lambda *_args: True)
    monkeypatch.setattr(service, "process_created_epoch", lambda _pid: 1000.0)
    monkeypatch.setattr(service, "_tailscale_register", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        service,
        "tailscale_dns_name",
        lambda: (_ for _ in ()).throw(service.ArtifactError("status failed")),
    )
    monkeypatch.setattr(service, "tailscale_off", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        service, "terminate_pid", lambda *_args: pytest.fail("server must stay tracked")
    )

    with pytest.raises(service.ArtifactError, match="status failed"):
        service.start_serve(tmp_path, ttl_seconds=60, funnel=False, slug="recovery")

    assert [item.slug for item in store.load_serves()] == ["recovery"]


def test_pid_birth_epoch_is_passed_to_termination(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, float]] = []
    monkeypatch.setattr(
        service,
        "terminate_tree_sync",
        lambda pid, *, created_at_epoch, **_kwargs: calls.append((pid, created_at_epoch)),
    )
    service.terminate_pid(123, 456.5)
    assert calls == [(123, 456.5)]


def test_concurrent_serves_are_serialized_without_lost_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "serves.json"
    monkeypatch.setattr(store, "serves_path", lambda: state_path)
    monkeypatch.setattr(service, "tailscale_serve_ports", lambda: set())
    monkeypatch.setattr(service, "pick_port", lambda occupied: min({50000, 50001} - set(occupied)))

    class FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

    monkeypatch.setattr(
        service, "launch_http_server", lambda _directory, port, _slug: FakeProcess(port)
    )
    monkeypatch.setattr(service, "wait_for_http_server", lambda _proc, _port: True)
    monkeypatch.setattr(service, "process_created_epoch", float)
    monkeypatch.setattr(service, "_tailscale_register", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "tailscale_dns_name", lambda: "host.ts.net")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                service.start_serve,
                tmp_path,
                ttl_seconds=60,
                funnel=False,
                slug=slug,
            )
            for slug in ("one", "two")
        ]
        for future in futures:
            future.result()

    serves = store.load_serves()
    assert {item.slug for item in serves} == {"one", "two"}
    assert {item.local_port for item in serves} == {50000, 50001}


def test_routed_cli_list_format_json_emits_json(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state_path = home / "serves.json"
    state_path.write_text(
        json.dumps(
            [
                {
                    "slug": "report",
                    "local_port": 50000,
                    "dir": "/tmp",
                    "work_id": None,
                    "created": "2026-01-01T00:00:00Z",
                    "ttl_seconds": 60,
                    "funnel": False,
                    "pid": 123,
                }
            ]
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["MERIDIAN_HOME"] = str(home)

    result = subprocess.run(
        [sys.executable, "-m", "meridian", "artifact", "list", "--format", "json"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)[0]["slug"] == "report"
