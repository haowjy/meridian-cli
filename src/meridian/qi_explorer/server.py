"""stdlib HTTP server for qi explore."""

from __future__ import annotations

import importlib.resources
import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import ParseResult, parse_qs, urlparse

from meridian.lib.kg.types import AnalysisResult
from meridian.qi_explorer.content import build_content_payload, serve_file
from meridian.qi_explorer.discovery import DiscoveryResult
from meridian.qi_explorer.graph_api import GraphIndex, build_graph_data


def _resolve_index_html() -> Path:
    package_file = Path(__file__).resolve().parent / "index.html"
    if package_file.is_file():
        return package_file

    ref = importlib.resources.files("meridian.qi_explorer") / "index.html"
    with importlib.resources.as_file(ref) as extracted:
        return Path(extracted)


class ExplorerState:
    """Mutable graph state guarded for rescans."""

    def __init__(self, discovery: DiscoveryResult) -> None:
        self.discovery = discovery
        self._lock = threading.Lock()
        self.graph: dict[str, Any]
        self.index: GraphIndex
        self.analysis: AnalysisResult
        self.graph, self.index, self.analysis = build_graph_data(discovery)

    @property
    def node_count(self) -> int:
        return int(self.graph.get("nodeCount", 0))

    @property
    def edge_count(self) -> int:
        return int(self.graph.get("edgeCount", 0))

    @property
    def is_empty(self) -> bool:
        return self.node_count == 0

    def rescan(self) -> dict[str, Any]:
        with self._lock:
            self.graph, self.index, self.analysis = build_graph_data(self.discovery)
            return self.graph

    def snapshot(self) -> tuple[dict[str, Any], GraphIndex]:
        with self._lock:
            return self.graph, self.index


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: object) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _text_response(
    handler: BaseHTTPRequestHandler,
    status: int,
    body: bytes,
    content_type: str,
) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def create_server(
    discovery: DiscoveryResult,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    state: ExplorerState | None = None,
) -> ThreadingHTTPServer:
    """Create a bound ``ThreadingHTTPServer`` for qi explore."""

    explorer_state = state or ExplorerState(discovery)
    index_path = _resolve_index_html()

    class QiExplorerHandler(BaseHTTPRequestHandler):
        server_state = explorer_state
        index_html_path = index_path

        def log_message(self, format: str, *args: object) -> None:
            _ = (format, args)

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            route = parsed.path

            if route == "/":
                self._serve_index()
                return
            if route == "/api/graph":
                graph, _ = self.server_state.snapshot()
                _json_response(self, HTTPStatus.OK, graph)
                return
            if route == "/api/content":
                self._serve_content(parsed)
                return
            if route == "/api/file":
                self._serve_file(parsed)
                return

            _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/api/rescan":
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            graph = self.server_state.rescan()
            _json_response(self, HTTPStatus.OK, graph)

        def _serve_index(self) -> None:
            try:
                body = self.index_html_path.read_bytes()
            except OSError:
                body = b"<html><body>qi explore</body></html>"
            _text_response(self, HTTPStatus.OK, body, "text/html; charset=utf-8")

        def _serve_content(self, parsed: ParseResult) -> None:
            node_id = parse_qs(parsed.query).get("id", [None])[0]
            if not node_id:
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "unknown node"})
                return
            _, index = self.server_state.snapshot()
            payload = build_content_payload(node_id, index)
            if payload is None:
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "unknown node"})
                return
            _json_response(self, HTTPStatus.OK, payload)

        def _serve_file(self, parsed: ParseResult) -> None:
            file_id = parse_qs(parsed.query).get("path", [None])[0]
            if not file_id:
                _json_response(self, HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return
            _, index = self.server_state.snapshot()
            result = serve_file(file_id, index)
            if result.forbidden:
                _json_response(self, HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return
            if result.kind == "not-found":
                _json_response(
                    self,
                    HTTPStatus.OK,
                    {"path": file_id, "content": "", "kind": "not-found"},
                )
                return
            _json_response(
                self,
                HTTPStatus.OK,
                {"path": result.path, "content": result.content, "kind": result.kind},
            )

    httpd = ThreadingHTTPServer((host, port), QiExplorerHandler)
    httpd.explorer_state = explorer_state
    return httpd


__all__ = ["ExplorerState", "create_server"]
