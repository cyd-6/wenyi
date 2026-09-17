"""Exercise mounted API auth, SPA routing and native progress on the actual host app."""

import sys
from dataclasses import replace

import pytest
import test_native_runtime as native_tests
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_native_runtime import source_project

native_env = native_tests.native_env


def test_desktop_host_auth_spa_and_websocket(native_env, tmp_path, monkeypatch):
    from wenyi_api.desktop.server import create_desktop_app
    from wenyi_api.runtime.progress import publish

    pool, root, settings = native_env
    authenticated = replace(settings, api_token="local-test-token")
    for module in list(sys.modules.values()):
        if (
            getattr(module, "__name__", "").startswith("wenyi_api")
            and getattr(module, "settings", None) is settings
        ):
            monkeypatch.setattr(module, "settings", authenticated)
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<html>Wenyi SPA</html>", encoding="utf-8")
    (web / "app.js").write_text("const ready=true", encoding="utf-8")
    pid, _ = source_project(root)
    publish({"project_id": pid, "kind": "progress", "done": 3, "total": 5, "run_id": "local"})
    app = create_desktop_app(web)
    # The fixture owns the test pool; lifespan is covered by the process smoke test.
    client = TestClient(app)
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/projects").status_code == 401
    assert (
        client.get(
            "/api/projects", headers={"Authorization": "Bearer local-test-token"}
        ).status_code
        == 200
    )
    assert client.get("/transfers").text == "<html>Wenyi SPA</html>"
    assert client.get(f"/projects/{pid}").status_code == 200
    assert client.get("/app.js").headers["content-type"].startswith("text/javascript")
    assert client.get("/assets/missing.js").status_code == 404
    assert client.get("/", headers={"Host": "untrusted.example"}).status_code == 400
    with client.websocket_connect(f"/ws/projects/{pid}/progress") as socket:
        socket.send_json({"token": "wrong"})
        with pytest.raises(WebSocketDisconnect) as error:
            socket.receive_json()
        assert error.value.code == 1008
    with client.websocket_connect(f"/ws/projects/{pid}/progress") as socket:
        socket.send_json({"token": "local-test-token"})
        assert socket.receive_json()["kind"] == "snapshot"
        assert socket.receive_json()["done"] == 3
