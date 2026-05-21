from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import meridian.lib.chat.server as server
from meridian.lib.chat.server import app, configure
from meridian.lib.core.types import SpawnId


class Handle:
    spawn_id = SpawnId("p-test")

    def health(self) -> bool:
        return True

    async def send_message(self, text: str) -> None:
        self.text = text

    async def send_cancel(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class Acquisition:
    async def acquire(
        self,
        chat_id: str,
        initial_prompt: str,
        *,
        execution_generation: int = 0,
        chat_state: str = "active",
    ) -> Handle:
        _ = (chat_id, initial_prompt, execution_generation, chat_state)
        return Handle()


def _replayed_events(client: TestClient, chat_id: str) -> list[dict[str, object]]:
    stream_source = server._runtime.get_stream_source(chat_id)
    assert stream_source is not None
    expected_event_count = len(list(stream_source.event_log.read_all()))
    with client.websocket_connect(f"/ws/chat/{chat_id}") as ws:
        return [ws.receive_json() for _ in range(expected_event_count)]


def test_rest_routes_are_command_wrappers(tmp_path: Path) -> None:
    configure(runtime_root=tmp_path, backend_acquisition=Acquisition())
    with TestClient(app) as client:
        created = client.post("/chat", json={}).json()
        chat_id = created["chat_id"]
        assert created["state"] == "idle"

        assert client.get(f"/chat/{chat_id}/state").json()["state"] == "idle"
        assert client.post(f"/chat/{chat_id}/msg", json={"text": "hi"}).json() == {
            "status": "accepted",
            "error": None,
        }
        assert client.post(f"/chat/{chat_id}/cancel").json()["status"] == "accepted"
        assert client.post(f"/chat/{chat_id}/close").json()["status"] == "accepted"
        rejected = client.post(f"/chat/{chat_id}/msg", json={"text": "after"}).json()
        assert rejected["error"] == "chat_closed"
        assert client.get(f"/chat/{chat_id}/state").json()["state"] == "closed"


def test_list_chats_and_events_routes_expose_persisted_state(tmp_path: Path) -> None:
    configure(runtime_root=tmp_path, backend_acquisition=Acquisition())
    with TestClient(app) as client:
        chat_id = client.post("/chat", json={}).json()["chat_id"]

        listed = client.get("/chat").json()
        assert listed["chats"] == [
            {"chat_id": chat_id, "state": "idle", "created_at": listed["chats"][0]["created_at"]}
        ]
        assert listed["chats"][0]["created_at"]

        events = client.get(f"/chat/{chat_id}/events").json()
        assert events["chat_id"] == chat_id
        assert [event["type"] for event in events["events"]] == ["chat.started"]

        prompted = client.post(f"/chat/{chat_id}/msg", json={"text": "hi"}).json()
        assert prompted["status"] == "accepted"
        limited = client.get(f"/chat/{chat_id}/events?last=1").json()
        assert len(limited["events"]) == 1
        assert limited["events"][0]["type"]


def _frontend_assets(tmp_path: Path):
    from meridian.lib.chat.frontend import FrontendAssets

    root = tmp_path / "dist"
    assets_dir = root / "assets"
    assets_dir.mkdir(parents=True)
    index = root / "index.html"
    index.write_text("<html><body>SPA</body></html>", encoding="utf-8")
    (assets_dir / "app.js").write_text("console.log('spa')", encoding="utf-8")
    return FrontendAssets(root=root, index_html=index, assets_dir=assets_dir)


def test_frontend_mount_serves_root_spa_and_assets(tmp_path: Path) -> None:
    from meridian.lib.chat.server import mount_frontend

    configure(runtime_root=tmp_path / "runtime", backend_acquisition=Acquisition())
    mount_frontend(app, _frontend_assets(tmp_path))

    with TestClient(app) as client:
        root_response = client.get("/")
        assert root_response.status_code == 200
        assert root_response.headers["content-type"].startswith("text/html")
        assert "SPA" in root_response.text

        asset_response = client.get("/assets/app.js")
        assert asset_response.status_code == 200
        # Accept any JS MIME type; Windows registry may return application/javascript
        assert "javascript" in asset_response.headers["content-type"]
        assert "console.log" in asset_response.text


def test_frontend_mount_preserves_api_priority_and_spa_catchall(tmp_path: Path) -> None:
    from meridian.lib.chat.server import mount_frontend

    configure(runtime_root=tmp_path / "runtime", backend_acquisition=Acquisition())
    mount_frontend(app, _frontend_assets(tmp_path))

    with TestClient(app) as client:
        created = client.post("/chat", json={})
        assert created.status_code == 200
        chat_id = created.json()["chat_id"]
        assert client.get(f"/chat/{chat_id}/state").json()["state"] == "idle"
        with client.websocket_connect(f"/ws/chat/{chat_id}") as ws:
            assert ws.receive_json()["type"] == "chat.started"

        fallback = client.get("/nested/client/route")
        assert fallback.status_code == 200
        assert fallback.headers["content-type"].startswith("text/html")
        assert "SPA" in fallback.text


def test_frontend_mount_is_idempotent(tmp_path: Path) -> None:
    from meridian.lib.chat.server import mount_frontend

    configure(runtime_root=tmp_path / "runtime", backend_acquisition=Acquisition())
    first_assets = _frontend_assets(tmp_path / "first")
    second_assets = _frontend_assets(tmp_path / "second")
    second_assets.index_html.write_text("<html><body>Second SPA</body></html>", encoding="utf-8")

    mount_frontend(app, first_assets)
    mount_frontend(app, second_assets)

    route_names = [getattr(route, "name", None) for route in app.router.routes]
    assert route_names.count("frontend-assets") == 1
    assert route_names.count("spa_fallback") == 1

    with TestClient(app) as client:
        fallback = client.get("/nested/client/route")
        assert fallback.status_code == 200
        assert "Second SPA" in fallback.text
