"""Artifact service logic and patched CLI workflow."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection
from pathlib import Path

import pytest

from meridian.cli.app_tree import artifact_app
from meridian.cli.artifact_cmd import (
    cmd_artifact_gc,  # noqa: F401 -- imports register commands on artifact_app
)
from meridian.lib.artifact import service, store
from meridian.lib.artifact.store import Serve


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("2h", 7200), ("30m", 1800), ("90s", 90), ("1d", 86400)],
)
def test_parse_ttl(value: str, seconds: int) -> None:
    assert service.parse_ttl(value) == seconds


@pytest.mark.parametrize("value", ["", "2", "1w", "0h", "-1m", "1.5h"])
def test_parse_ttl_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        service.parse_ttl(value)


def test_expiry_is_strict_and_persistent_never_expires() -> None:
    created = datetime(2026, 1, 1, tzinfo=UTC)
    base = Serve("report", 50000, "/tmp", None, "2026-01-01T00:00:00Z", 60, False, 123)
    persistent = Serve("keep", 50001, "/tmp", None, base.created, None, False, 124)

    assert not service.is_expired(base, created + timedelta(seconds=60))
    assert service.is_expired(base, created + timedelta(seconds=61))
    assert not service.is_expired(persistent, created + timedelta(days=365))


def test_pick_port_skips_occupied_and_failed_bind_probe() -> None:
    candidates = iter([50000, 50001, 50002])

    port = service.pick_port(
        {50000},
        lambda candidate: candidate != 50001,
        choose=lambda _low, _high: next(candidates),
    )

    assert port == 50002


def test_slug_derivation_sanitizes_and_resolves_collisions() -> None:
    assert service.derive_slug(Path("/tmp/My  Report!!"), None, set(), 50000) == "my-report"
    assert service.derive_slug(Path("/tmp/report"), None, {"report"}, 51234) == "report-51234"
    assert (
        service.derive_slug(
            Path("/tmp/report"), None, {"report", "report-51234"}, 51234
        )
        == "report-51234-2"
    )
    with pytest.raises(service.ArtifactError):
        service.derive_slug(Path("/"), "/", set(), 50000)


def test_teardown_is_surgically_scoped_to_slug(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def fake_tailscale(args: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service, "_tailscale", fake_tailscale)
    assert service.tailscale_off("my-report", 50000, funnel=False)

    assert commands == [
        ["serve", "--https=50000", "--set-path=/my-report", "off"]
    ]


def test_register_and_url_use_explicit_listener_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_tailscale(args: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service, "_tailscale", fake_tailscale)
    monkeypatch.setattr(service, "tailscale_dns_name", lambda: "host.ts.net")

    service._tailscale_register(54321, "my-report", funnel=False)

    assert commands == [
        [
            "serve",
            "--bg",
            "--https=54321",
            "--set-path",
            "/my-report",
            "http://127.0.0.1:54321",
        ]
    ]
    assert service.build_url("my-report", 54321) == "https://host.ts.net:54321/my-report/"


def test_funnel_uses_fixed_public_port_for_register_teardown_and_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def fake_tailscale(args: list[str]) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(service, "_tailscale", fake_tailscale)
    monkeypatch.setattr(service, "tailscale_dns_name", lambda: "host.ts.net")
    public_port = service.tailnet_port(54321, funnel=True)

    service._tailscale_register(54321, "public-report", funnel=True)
    assert service.tailscale_off("public-report", public_port, funnel=True)

    assert public_port == 10000
    assert commands == [
        [
            "funnel",
            "--bg",
            "--https=10000",
            "--set-path",
            "/public-report",
            "http://127.0.0.1:54321",
        ],
        ["funnel", "--https=10000", "--set-path=/public-report", "off"],
    ]
    assert (
        service.build_url("public-report", public_port)
        == "https://host.ts.net:10000/public-report/"
    )


def test_tailscale_listener_ports_are_excluded_from_port_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps({"TCP": {"50000": {}, "8443": {}}})
    monkeypatch.setattr(
        service,
        "_tailscale",
        lambda args: subprocess.CompletedProcess(args, 0, payload, ""),
    )
    candidates = iter([50000, 50001])

    port = service.pick_port(
        service.tailscale_serve_ports(),
        lambda _port: True,
        choose=lambda _low, _high: next(candidates),
    )

    assert port == 50001


class _FakeProcess:
    pid = 4242

    def poll(self) -> None:
        return None


def _invoke_artifact(args: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        artifact_app(args)
    assert exit_info.value.code == 0


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


@pytest.mark.parametrize("port", [80, 443])
def test_cli_rejects_well_known_explicit_port_before_serving(
    port: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        service, "start_serve", lambda *_args, **_kwargs: pytest.fail("must not serve")
    )

    with pytest.raises(SystemExit) as exit_info:
        artifact_app(["serve", str(tmp_path), "--port", str(port)])

    assert exit_info.value.code == 2
    assert "--port must be >= 1024" in capsys.readouterr().err


@pytest.mark.parametrize("port", [0, 65536])
def test_cli_rejects_out_of_range_explicit_port(
    port: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        service, "start_serve", lambda *_args, **_kwargs: pytest.fail("must not serve")
    )
    with pytest.raises(SystemExit) as exit_info:
        artifact_app(["serve", str(tmp_path), "--port", str(port)])
    assert exit_info.value.code == 2


def test_explicit_port_already_in_state_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_path = tmp_path / "serves.json"
    monkeypatch.setattr(store, "serves_path", lambda: state_path)
    store.save_serves([_serve("existing", 55000, ttl=None)])
    monkeypatch.setattr(service, "tailscale_serve_ports", lambda: set())
    monkeypatch.setattr(service, "local_port_available", lambda _port: True)
    monkeypatch.setattr(
        service, "launch_http_server", lambda *_args: pytest.fail("must not launch")
    )

    with pytest.raises(SystemExit) as exit_info:
        artifact_app(["serve", str(tmp_path), "--slug", "new", "--port", "55000"])

    assert exit_info.value.code == 2
    assert "port 55000 is already in use" in capsys.readouterr().err


def test_explicit_tailscale_listener_port_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store, "serves_path", lambda: tmp_path / "serves.json")
    monkeypatch.setattr(service, "tailscale_serve_ports", lambda: {55000})
    monkeypatch.setattr(service, "local_port_available", lambda _port: True)
    monkeypatch.setattr(
        service, "launch_http_server", lambda *_args: pytest.fail("must not launch")
    )

    with pytest.raises(service.ArtifactError, match="port 55000 is already in use"):
        service.start_serve(
            tmp_path, ttl_seconds=60, funnel=False, slug="new", port=55000
        )


def test_explicit_locally_bound_port_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(store, "serves_path", lambda: tmp_path / "serves.json")
    monkeypatch.setattr(service, "tailscale_serve_ports", lambda: set())
    monkeypatch.setattr(service, "local_port_available", lambda _port: False)
    monkeypatch.setattr(
        service, "launch_http_server", lambda *_args: pytest.fail("must not launch")
    )

    with pytest.raises(service.ArtifactError, match="port 55000 is already in use"):
        service.start_serve(
            tmp_path, ttl_seconds=60, funnel=False, slug="new", port=55000
        )


def test_free_explicit_port_is_used_for_bind_listener_and_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "serves.json"
    launched: list[tuple[int, str]] = []
    registered: list[tuple[int, str]] = []
    monkeypatch.setattr(store, "serves_path", lambda: state_path)
    monkeypatch.setattr(service, "tailscale_serve_ports", lambda: set())
    monkeypatch.setattr(service, "local_port_available", lambda _port: True)
    monkeypatch.setattr(
        service,
        "pick_port",
        lambda _occupied: pytest.fail("explicit port must bypass random selection"),
    )

    class FakeProcess:
        pid = 55000

    def fake_launch(_directory: Path, port: int, slug: str) -> FakeProcess:
        launched.append((port, slug))
        return FakeProcess()

    monkeypatch.setattr(service, "launch_http_server", fake_launch)
    monkeypatch.setattr(service, "wait_for_http_server", lambda *_args: True)
    monkeypatch.setattr(service, "process_created_epoch", lambda _pid: 1000.0)
    monkeypatch.setattr(
        service,
        "_tailscale_register",
        lambda port, slug, *, funnel: registered.append((port, slug)),
    )
    monkeypatch.setattr(service, "tailscale_dns_name", lambda: "host.ts.net")

    serve, url = service.start_serve(
        tmp_path, ttl_seconds=60, funnel=False, slug="report", port=55000
    )

    assert serve.local_port == 55000
    assert launched == [(55000, "report")]
    assert registered == [(55000, "report")]
    assert url == "https://host.ts.net:55000/report/"


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


@pytest.mark.integration
def test_patched_cli_serve_list_stop_gc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state_path = tmp_path / "serves.json"
    terminated: list[int] = []
    disabled: list[str] = []
    monkeypatch.setattr(store, "serves_path", lambda: state_path)
    monkeypatch.setattr(service, "pick_port", lambda _occupied: 54321)
    monkeypatch.setattr(service, "tailscale_serve_ports", lambda: set())
    monkeypatch.setattr(
        service, "launch_http_server", lambda _directory, _port, _slug: _FakeProcess()
    )
    monkeypatch.setattr(service, "wait_for_http_server", lambda _proc, _port: True)
    monkeypatch.setattr(service, "process_created_epoch", lambda _pid: 1000.0)
    monkeypatch.setattr(service, "_tailscale_register", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "tailscale_dns_name", lambda: "host.example.ts.net")
    monkeypatch.setattr(service, "pid_alive", lambda _pid, _created: True)
    monkeypatch.setattr(
        service, "terminate_pid", lambda pid, _created: terminated.append(pid)
    )
    monkeypatch.setattr(
        service,
        "tailscale_off",
        lambda slug, _port, *, funnel: disabled.append(slug) is None,
    )
    monkeypatch.setattr(service.time, "sleep", lambda _seconds: None)

    _invoke_artifact(["serve", str(tmp_path), "--slug", "my-report"])
    assert "https://host.example.ts.net:54321/my-report/" in capsys.readouterr().out
    assert len(store.load_serves()) == 1

    _invoke_artifact(["list"])
    listed = capsys.readouterr().out
    assert "my-report" in listed
    assert "54321" in listed
    assert str(tmp_path) in listed

    _invoke_artifact(["stop", "my-report"])
    assert "Stopped" in capsys.readouterr().out
    assert store.load_serves() == []
    assert terminated == [4242]
    assert disabled == ["my-report"]

    _invoke_artifact(["gc"])
    assert "Garbage-collected 0" in capsys.readouterr().out


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
    monkeypatch.setattr(
        service, "_tailscale", lambda _args: pytest.fail("tailscale must not run")
    )
    with pytest.raises(service.ArtifactError):
        service._tailscale_register(50000, "", funnel=False)
    with pytest.raises(service.ArtifactError):
        service.tailscale_off("/", 50000, funnel=False)


@pytest.mark.parametrize(
    "changes",
    [
        {"pid": 0},
        {"local_port": 0},
        {"local_port": 65536},
        {"ttl_seconds": 0},
        {"process_created_at": -1.0},
    ],
)
def test_invalid_process_state_is_dropped(
    changes: dict[str, object], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "serves.json"
    payload = {
        "slug": "report",
        "local_port": 50000,
        "dir": "/tmp",
        "work_id": None,
        "created": "2026-01-01T00:00:00Z",
        "ttl_seconds": 60,
        "funnel": False,
        "pid": 123,
        "process_created_at": 1000.0,
    }
    payload.update(changes)
    state_path.write_text(json.dumps([payload]), encoding="utf-8")
    monkeypatch.setattr(store, "serves_path", lambda: state_path)
    assert store.load_serves() == []


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
        service.start_serve(
            tmp_path, ttl_seconds=60, funnel=False, slug="recovery"
        )

    assert [item.slug for item in store.load_serves()] == ["recovery"]


def test_gc_continues_after_one_teardown_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "serves.json"
    monkeypatch.setattr(store, "serves_path", lambda: state_path)
    store.save_serves([_serve("fails", 50000), _serve("removed", 50001, pid=4243)])
    monkeypatch.setattr(
        service, "tailscale_off", lambda slug, _port, *, funnel: slug == "removed"
    )
    terminated: list[tuple[int, float | None]] = []
    monkeypatch.setattr(
        service, "terminate_pid", lambda pid, created: terminated.append((pid, created))
    )

    removed, failed = service.sweep_expired(now=datetime(2026, 1, 2, tzinfo=UTC))

    assert [item.slug for item in removed] == ["removed"]
    assert failed == ["fails"]
    assert [item.slug for item in store.load_serves()] == ["fails"]
    assert terminated == [(4243, 1000.0)]


def test_pid_birth_epoch_is_passed_to_termination(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, float]] = []
    monkeypatch.setattr(
        service,
        "terminate_tree_sync",
        lambda pid, *, created_at_epoch, **_kwargs: calls.append((pid, created_at_epoch)),
    )
    service.terminate_pid(123, 456.5)
    assert calls == [(123, 456.5)]


def test_start_retries_when_first_local_server_never_becomes_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "serves.json"
    ports = iter([50000, 50001])
    readiness = iter([False, True])
    terminated: list[int] = []
    launched: list[tuple[int, str]] = []
    monkeypatch.setattr(store, "serves_path", lambda: state_path)
    monkeypatch.setattr(service, "tailscale_serve_ports", lambda: set())
    monkeypatch.setattr(service, "pick_port", lambda _occupied: next(ports))

    class FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

    def fake_launch(_directory: Path, port: int, slug: str) -> FakeProcess:
        launched.append((port, slug))
        return FakeProcess(port)

    monkeypatch.setattr(service, "launch_http_server", fake_launch)
    monkeypatch.setattr(
        service, "wait_for_http_server", lambda _proc, _port: next(readiness)
    )
    monkeypatch.setattr(service, "process_created_epoch", float)
    monkeypatch.setattr(
        service, "terminate_pid", lambda pid, _created: terminated.append(pid)
    )
    monkeypatch.setattr(service, "_tailscale_register", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "tailscale_dns_name", lambda: "host.ts.net")

    serve, _url = service.start_serve(
        tmp_path, ttl_seconds=60, funnel=False, slug="retry"
    )

    assert serve.local_port == 50001
    assert terminated == [50000]
    assert launched == [(50000, "retry"), (50001, "retry")]


def test_concurrent_serves_are_serialized_without_lost_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "serves.json"
    monkeypatch.setattr(store, "serves_path", lambda: state_path)
    monkeypatch.setattr(service, "tailscale_serve_ports", lambda: set())
    monkeypatch.setattr(
        service, "pick_port", lambda occupied: min({50000, 50001} - set(occupied))
    )

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


def test_list_json_is_local_and_emits_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_path = tmp_path / "serves.json"
    monkeypatch.setattr(store, "serves_path", lambda: state_path)
    store.save_serves([_serve("report", 50000)])
    monkeypatch.setattr(
        service,
        "tailscale_dns_name",
        lambda: pytest.fail("JSON listing must not call tailscale"),
    )
    _invoke_artifact(["list", "--format", "json"])
    assert json.loads(capsys.readouterr().out)[0]["slug"] == "report"


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


def test_list_survives_unavailable_tailscale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_path = tmp_path / "serves.json"
    monkeypatch.setattr(store, "serves_path", lambda: state_path)
    store.save_serves([_serve("report", 50000)])
    monkeypatch.setattr(
        service,
        "tailscale_dns_name",
        lambda: (_ for _ in ()).throw(service.ArtifactError("offline")),
    )
    monkeypatch.setattr(service, "pid_alive", lambda *_args: True)
    _invoke_artifact(["list"])
    output = capsys.readouterr()
    assert "report" in output.out
    assert "unavailable" in output.out
    assert output.err.count("offline") == 1
