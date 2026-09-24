import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from app.api.three_xui import ThreeXUIClient


def test_panel_client_ignores_system_proxy_by_default(monkeypatch) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = json.dumps({"info": {"version": "test"},
                               "paths": {path: {} for path in ThreeXUIClient.REQUIRED_PATHS}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")

    async def request() -> str:
        client = ThreeXUIClient({"url": f"http://127.0.0.1:{server.server_port}"}, timeout=2)
        try:
            spec = await client.discover()
            return str(spec["info"]["version"])
        finally:
            await client.aclose()

    try:
        assert asyncio.run(request()) == "test"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_optional_xray_logs_require_discovered_post_and_handle_null() -> None:
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(f"{request.method} {request.url.path}")
        return httpx.Response(200, json={"success": True, "obj": None})

    async def scenario() -> None:
        client = ThreeXUIClient({"url": "http://127.0.0.1"})
        await client.client.aclose()
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            assert await client.xray_logs() is None
            client.openapi = {"paths": {client.XRAY_LOG_PATH: {"get": {}}}}
            assert await client.xray_logs() is None
            assert requests == []
            client.openapi = {"paths": {client.XRAY_LOG_PATH: {"post": {}}}}
            assert await client.xray_logs(3) is None
            assert requests == ["POST /panel/api/server/xraylogs/3"]
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_optional_xray_logs_return_bounded_in_memory_text() -> None:
    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"success": True, "obj": "a" * 70000 + "reality failed"})

    async def scenario() -> None:
        client = ThreeXUIClient({"url": "http://127.0.0.1"})
        await client.client.aclose()
        client.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        client.openapi = {"paths": {client.XRAY_LOG_PATH: {"post": {}}}}
        try:
            result = await client.xray_logs()
            assert result is not None
            assert len(result) == 64 * 1024
            assert result.endswith("reality failed")
        finally:
            await client.aclose()

    asyncio.run(scenario())
