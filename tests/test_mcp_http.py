"""The real HTTP server: token checks, scopes, a full tool call over MCP, uploads.

Starts the app with uvicorn on a free local port and talks to it with the
official MCP client, the same way a Hermes sandbox would (minus HTTPS).
"""

from __future__ import annotations

import asyncio
import io
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from PIL import Image

from conftest import RAMESH
from order_desk.mcp_server.server import create_app

DEALER_TOKEN, SHOP_TOKEN = "dealer-test-token", "shop-test-token"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(deps):
    from dataclasses import replace

    port = _free_port()
    settings = replace(deps.settings, dealer_mcp_token=DEALER_TOKEN, shop_mcp_token=SHOP_TOKEN, mcp_port=port)
    deps.settings = settings
    config = uvicorn.Config(create_app(settings, deps), host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health").status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=5)


async def _call(base: str, path: str, token: str, tool: str | None, args: dict | None = None):
    import httpx2
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    client = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30)
    async with client:
        async with streamable_http_client(f"{base}{path}", http_client=client) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                if tool is None:
                    return [t.name for t in listed.tools]
                result = await session.call_tool(tool, args or {})
                return json.loads(result.content[0].text)


def test_health_needs_no_token(server):
    assert httpx.get(f"{server}/health").json() == {"ok": True}


def test_wrong_or_missing_token_is_refused(server):
    assert httpx.post(f"{server}/dealer/mcp", json={}).status_code == 401
    assert httpx.post(f"{server}/dealer/mcp", json={}, headers={"Authorization": f"Bearer {SHOP_TOKEN}"}).status_code == 401
    assert httpx.post(f"{server}/shop/mcp", json={}, headers={"Authorization": f"Bearer {DEALER_TOKEN}"}).status_code == 401


def test_dealer_scope_cannot_see_owner_tools(server):
    dealer_tools = asyncio.run(_call(server, "/dealer/mcp", DEALER_TOKEN, None))
    shop_tools = asyncio.run(_call(server, "/shop/mcp", SHOP_TOKEN, None))
    assert "approve_order" not in dealer_tools
    assert "apply_stock_in" not in dealer_tools
    assert "approve_order" in shop_tools
    assert "confirm_order" not in shop_tools


def test_tool_call_over_mcp(server):
    result = asyncio.run(_call(server, "/dealer/mcp", DEALER_TOKEN, "get_dealer_profile",
                               {"telegram_user_id": RAMESH}))
    assert result["ok"] is True and result["status"] == "active"


def test_tool_error_comes_back_as_readable_result(server):
    result = asyncio.run(_call(server, "/dealer/mcp", DEALER_TOKEN, "start_order",
                               {"telegram_user_id": 900000004, "source_kind": "photo", "source_ref": "m1"}))
    assert result == {"ok": False, "error": "not_allowed",
                      "message": "The shop does not take orders from this account now. Please call the shop."}


def test_upload_returns_image_ref(server):
    buf = io.BytesIO()
    Image.new("RGB", (10, 10), (1, 2, 3)).save(buf, format="PNG")
    resp = httpx.post(f"{server}/uploads", content=buf.getvalue(),
                      headers={"Authorization": f"Bearer {DEALER_TOKEN}", "Content-Type": "image/png"})
    assert resp.status_code == 200
    assert resp.json()["image_ref"].startswith("up:up_")
    bad = httpx.post(f"{server}/uploads", content=b"not an image",
                     headers={"Authorization": f"Bearer {DEALER_TOKEN}"})
    assert bad.status_code == 400
