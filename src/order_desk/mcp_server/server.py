"""The Order Desk MCP server: one HTTP app, two MCP endpoints, one upload route.

    POST /dealer/mcp   MCP (streamable HTTP), dealer tools, needs DEALER_MCP_TOKEN
    POST /shop/mcp     MCP (streamable HTTP), shop tools,   needs SHOP_MCP_TOKEN
    POST /uploads      raw image bytes -> {"image_ref": "up:..."}, needs either token
    GET  /health       no token; returns {"ok": true}

NemoClaw requires HTTPS for MCP servers and refuses localhost. This app
serves plain HTTP on 127.0.0.1; a tunnel in front of it gives HTTPS
(see docs/08-nemoclaw-setup.md). The bearer token check happens here, in
BearerAuth, for every request.

Run it with:  order-desk serve
"""

from __future__ import annotations

import contextlib
import hmac
import logging

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from order_desk.config import Settings
from order_desk.domain.errors import OrderDeskError
from order_desk.mcp_server.tools import ToolSpec, dealer_tools, shop_tools
from order_desk.services.dealer_desk import DealerDesk
from order_desk.services.deps import Deps
from order_desk.services.shop_desk import ShopDesk
from order_desk.vision.images import store

log = logging.getLogger(__name__)

DEALER_INSTRUCTIONS = (
    "Order desk for dealers of an electrical shop. The database is the only source of truth: "
    "never state stock, prices or order states from memory; call a tool. Send 'readback' texts as they are."
)
SHOP_INSTRUCTIONS = (
    "Order desk for the shop owner and packing team. The database is the only source of truth. "
    "Use get_report before writing SQL. Never approve, reject or apply stock-in without a clear instruction."
)


def build_mcp(name: str, instructions: str, specs: list[ToolSpec]) -> MCPServer:
    server = MCPServer(name=name, instructions=instructions)
    for spec in specs:
        server.add_tool(spec.fn, name=spec.name)
    return server


class BearerAuth:
    """ASGI middleware: each path prefix needs its own bearer token.

    Uses hmac.compare_digest so the check takes the same time for any wrong
    token (no timing hints). A missing token in the settings closes the path.
    """

    def __init__(self, app, tokens_by_prefix: dict[str, list[str]], open_paths: tuple[str, ...] = ("/health",)):
        self.app = app
        self.tokens_by_prefix = tokens_by_prefix
        self.open_paths = open_paths

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in self.open_paths:
            return await self.app(scope, receive, send)
        allowed = next((t for p, t in self.tokens_by_prefix.items() if scope["path"].startswith(p)), None)
        header = dict(scope.get("headers") or []).get(b"authorization", b"").decode("latin-1")
        given = header[7:] if header.lower().startswith("bearer ") else ""
        if not allowed or not given or not any(hmac.compare_digest(given, t) for t in allowed if t):
            response = JSONResponse({"error": "unauthorized"}, status_code=401,
                                    headers={"WWW-Authenticate": "Bearer"})
            return await response(scope, receive, send)
        return await self.app(scope, receive, send)


def create_app(settings: Settings, deps: Deps | None = None) -> Starlette:
    deps = deps or Deps.build(settings)
    deps.db.initialize()
    dealer_desk, shop_desk = DealerDesk(deps), ShopDesk(deps)

    dealer_specs = dealer_tools(dealer_desk)
    shop_specs = shop_tools(shop_desk, dealer_desk)
    dealer_mcp = build_mcp("order-desk-dealer", DEALER_INSTRUCTIONS, dealer_specs)
    shop_mcp = build_mcp("order-desk-shop", SHOP_INSTRUCTIONS, shop_specs)

    security = None
    if settings.mcp_allowed_hosts:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=settings.mcp_allowed_hosts + [f"127.0.0.1:{settings.mcp_port}", f"localhost:{settings.mcp_port}"],
            allowed_origins=[f"https://{h}" for h in settings.mcp_allowed_hosts],
        )
    # host="0.0.0.0" only stops the SDK from adding its localhost-only Host check;
    # the process still listens on settings.mcp_host (127.0.0.1 by default).
    common = dict(stateless_http=True, json_response=True, transport_security=security, host="0.0.0.0")
    dealer_app = dealer_mcp.streamable_http_app(**common)
    shop_app = shop_mcp.streamable_http_app(**common)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        # Mounted apps do not run their own lifespan, so we start both MCP session managers here.
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(dealer_mcp.session_manager.run())
            await stack.enter_async_context(shop_mcp.session_manager.run())
            log.info("MCP endpoints ready: /dealer/mcp (%d tools), /shop/mcp (%d tools)",
                     len(dealer_specs), len(shop_specs))
            yield

    async def health(_request: Request):
        return JSONResponse({"ok": True})

    async def upload(request: Request):
        body = await request.body()
        try:
            image = store(deps.db, settings.upload_dir, body, source="upload")
        except OrderDeskError as exc:
            return JSONResponse(exc.to_dict(), status_code=400)
        return JSONResponse({"ok": True, "image_ref": f"up:{image.upload_id}"})

    app = Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/uploads", upload, methods=["POST"]),
            Mount("/dealer", app=dealer_app),
            Mount("/shop", app=shop_app),
        ],
        lifespan=lifespan,
    )
    tokens = {
        "/dealer/": [settings.dealer_mcp_token],
        "/shop/": [settings.shop_mcp_token],
        "/uploads": [settings.dealer_mcp_token, settings.shop_mcp_token, settings.upload_token],
    }
    return BearerAuth(app, tokens)


def main(settings: Settings) -> None:
    import uvicorn

    if not settings.dealer_mcp_token or not settings.shop_mcp_token:
        raise SystemExit("Set DEALER_MCP_TOKEN and SHOP_MCP_TOKEN in .env (see README step 2).")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # uvicorn stops cleanly on SIGTERM: it stops new requests and lets running ones finish.
    uvicorn.run(create_app(settings), host=settings.mcp_host, port=settings.mcp_port,
                timeout_graceful_shutdown=20, log_level="info")


__all__ = ["create_app", "main"]
