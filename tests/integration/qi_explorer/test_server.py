"""Integration tests for qi explore HTTP endpoints."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path

from meridian.qi_explorer.discovery import DiscoveryResult, ScanRoot
from meridian.qi_explorer.server import create_server


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _request_json(url: str) -> tuple[int, dict[str, object]]:
    with urllib.request.urlopen(url) as response:
        body = response.read().decode("utf-8")
        return response.status, json.loads(body)


def _request_status(url: str) -> int:
    try:
        with urllib.request.urlopen(url) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_server_endpoints(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    beta = root / "beta"
    _write(
        root / "AGENTS.md",
        "\n".join(
            [
                "# Root",
                "",
                "<script>alert(1)</script>",
                "",
                "[beta](beta/AGENTS.md)",
                "[missing](nope.md)",
            ]
        ),
    )
    _write(beta / "AGENTS.md", "# Beta\n")
    _write(root / "notes.md", "# Notes\n")

    discovery = DiscoveryResult(
        primary=root.resolve(),
        roots=[ScanRoot(name="codebase", abs_path=root.resolve(), kind="primary")],
    )
    httpd = create_server(discovery, port=0)
    host, port = httpd.server_address
    base = f"http://{host}:{port}"

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        status, graph = _request_json(f"{base}/api/graph")
        assert status == HTTPStatus.OK
        assert graph["nodeCount"] == 2
        assert graph["edgeCount"] == 1
        assert "nodes" in graph
        assert "links" in graph
        assert "scanRoots" in graph

        node_id = "codebase:."
        status, content = _request_json(f"{base}/api/content?id={node_id}")
        assert status == HTTPStatus.OK
        agents_html = str(content["agentsHtml"])
        assert "<script>" not in agents_html
        assert "<img" not in agents_html
        assert 'data-category="cross-ref"' in agents_html
        assert "data-node-id=" in agents_html
        assert 'data-category="broken"' in agents_html

        status, file_payload = _request_json(
            f"{base}/api/file?path=codebase:notes.md",
        )
        assert status == HTTPStatus.OK
        assert file_payload["kind"] == "markdown"

        forbidden_status = _request_status(
            f"{base}/api/file?path=codebase:../../../etc/passwd",
        )
        assert forbidden_status == HTTPStatus.FORBIDDEN
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
