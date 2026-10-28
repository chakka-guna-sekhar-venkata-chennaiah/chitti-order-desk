"""Settings, read once from environment variables.

Every setting has a safe default for local development, except the secrets.
Copy `.env.example` to `.env` and fill in the secrets. See README.md, step 2.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines from a .env file into os.environ.

    We do not override variables that are already set, so a real environment
    variable always wins over the file. This avoids one more dependency.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def _int_list(value: str) -> list[int]:
    return [int(v) for v in value.replace(" ", "").split(",") if v]


@dataclass(frozen=True)
class Settings:
    # --- storage -------------------------------------------------------------
    db_path: Path = Path("data/order_desk.sqlite3")
    upload_dir: Path = Path("data/uploads")
    generated_dir: Path = Path("data/generated")       # PDFs and chart images

    # --- Nebius Token Factory (OpenAI-compatible) ----------------------------
    nebius_api_key: str = ""
    nebius_base_url: str = "https://api.tokenfactory.nebius.com/v1/"
    # Model IDs: copy the exact IDs from the Token Factory console.
    vision_model: str = "nvidia/Nemotron-3.5-Super-VL"       # reads chittis and invoices
    agent_model: str = "nvidia/Nemotron-3_5-Lightning"       # reference agent + evals
    # The vision model thinks before it answers, and the thinking counts against this limit (KI-060).
    vision_max_tokens: int = 16000

    # --- Telegram ------------------------------------------------------------
    dealer_bot_token: str = ""      # bot that dealers talk to
    shop_bot_token: str = ""        # bot that the owner and packers talk to

    # --- MCP server ----------------------------------------------------------
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8710
    dealer_mcp_token: str = ""      # bearer token for /dealer/mcp (dealer sandbox)
    shop_mcp_token: str = ""        # bearer token for /shop/mcp (shop sandbox)
    upload_token: str = ""          # optional token that may ONLY upload images (POST /uploads)
    # NemoClaw sandbox names: the server copies cached photos out of them.
    dealer_sandbox: str = "dealer-desk"
    shop_sandbox: str = "shop-desk"
    nemohermes_bin: str = "nemohermes"
    # Host names that may reach the MCP server (DNS-rebinding protection).
    # Example: "mymac.tail1234.ts.net". Empty = protection off.
    mcp_allowed_hosts: list[str] = field(default_factory=list)

    # --- business rules ------------------------------------------------------
    dealer_check_hours: int = 6          # AWAITING_DEALER expires after this
    owner_reminder_hours: int = 2        # remind the owner about waiting orders
    duplicate_window_hours: int = 6      # "same chitti again?" check window
    shop_name: str = "Sri Lakshmi Electricals"

    # --- staff (seeded into the staff table by `order-desk init`) ------------
    owner_telegram_ids: list[int] = field(default_factory=list)
    packer_telegram_ids: list[int] = field(default_factory=list)


def load_settings(env_file: str | os.PathLike[str] = ".env") -> Settings:
    """Build Settings from the environment (and an optional .env file)."""
    _load_dotenv(Path(env_file))
    e = os.environ.get
    return Settings(
        db_path=Path(e("ORDER_DESK_DB", "data/order_desk.sqlite3")),
        upload_dir=Path(e("ORDER_DESK_UPLOAD_DIR", "data/uploads")),
        generated_dir=Path(e("ORDER_DESK_GENERATED_DIR", "data/generated")),
        nebius_api_key=e("NEBIUS_API_KEY", ""),
        nebius_base_url=e("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1/"),
        vision_model=e("VISION_MODEL", "nvidia/Nemotron-3.5-Super-VL"),
        agent_model=e("AGENT_MODEL", "nvidia/Nemotron-3_5-Lightning"),
        vision_max_tokens=int(e("VISION_MAX_TOKENS", "16000")),
        dealer_bot_token=e("DEALER_BOT_TOKEN", ""),
        shop_bot_token=e("SHOP_BOT_TOKEN", ""),
        mcp_host=e("MCP_HOST", "127.0.0.1"),
        mcp_port=int(e("MCP_PORT", "8710")),
        dealer_mcp_token=e("DEALER_MCP_TOKEN", ""),
        shop_mcp_token=e("SHOP_MCP_TOKEN", ""),
        upload_token=e("UPLOAD_TOKEN", ""),
        dealer_sandbox=e("DEALER_SANDBOX", "dealer-desk"),
        shop_sandbox=e("SHOP_SANDBOX", "shop-desk"),
        nemohermes_bin=e("NEMOHERMES_BIN", "nemohermes"),
        mcp_allowed_hosts=[h for h in e("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()],
        dealer_check_hours=int(e("DEALER_CHECK_HOURS", "6")),
        owner_reminder_hours=int(e("OWNER_REMINDER_HOURS", "2")),
        duplicate_window_hours=int(e("DUPLICATE_WINDOW_HOURS", "6")),
        shop_name=e("SHOP_NAME", "Sri Lakshmi Electricals"),
        owner_telegram_ids=_int_list(e("OWNER_TELEGRAM_IDS", "")),
        packer_telegram_ids=_int_list(e("PACKER_TELEGRAM_IDS", "")),
    )


__all__ = ["Settings", "load_settings"]
