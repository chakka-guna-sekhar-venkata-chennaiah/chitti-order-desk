# 8. Setup on a Mac with NemoClaw and Hermes

This guide sets up the full system on one Apple Silicon Mac. Each step says
what to do and how to check it. Every NemoClaw and Hermes command comes from
their docs or source (links in 8.9).

## 8.1 What you need

| Item | Where |
|---|---|
| Apple Silicon Mac | NemoClaw: macOS (Apple Silicon) is "tested with limitations". Intel Macs are not supported. |
| Colima or Docker Desktop, running | With Homebrew Colima: `brew install colima docker`, then `colima start` |
| Xcode command line tools | `xcode-select --install` |
| Python 3.11+ | `python3 --version` |
| Nebius Token Factory API key | Token Factory console -> Get API key |
| Two Telegram bots | Telegram -> @BotFather -> `/newbot` (one "dealer" bot, one "shop" bot) |
| Your Telegram user ID | Telegram -> @userinfobot |
| A second Telegram account | For the demo: one account is the dealer, one is the owner |

## 8.2 Run the Order Desk on the Mac

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # fill in the values; use `order-desk new-token` for the three tokens
order-desk init                 # database + demo catalog + your staff IDs
pytest -q                       # all tests must pass
make local                      # MCP server + outbox sender + scheduler
curl http://127.0.0.1:8710/health   # -> {"ok": true}
```

## 8.3 Give the MCP server an HTTPS address

NemoClaw accepts only HTTPS MCP servers. It refuses `localhost`, loopback
addresses and `host.docker.internal`. So the server needs an HTTPS address
that the sandbox can reach.

**Option A: Cloudflare quick tunnel (simplest for a demo)**
```bash
brew install cloudflared
cloudflared tunnel --url http://127.0.0.1:8710
# prints https://<random-words>.trycloudflare.com
```
The address changes each time the tunnel starts. Then run `mcp add` again.
NemoClaw notes that public endpoints with rotating addresses need explicit pin
refreshes; check `nemohermes <name> mcp status order-desk` after a restart.

**Option B: Tailscale HTTPS (stable address)**
`tailscale serve` gives an `https://<machine>.<tailnet>.ts.net` address with a
valid certificate. Its IP is a private (CGNAT) address, so add the server
with `--trusted-private-host <machine>.<tailnet>.ts.net`. Test that the
sandbox can resolve the `ts.net` name before you rely on it.

Put the address in `.env` as `MCP_PUBLIC_URL=https://...` (used by the
setup script) and its host name in `MCP_ALLOWED_HOSTS`.

## 8.4 Install NemoClaw

```bash
export NEMOCLAW_DISABLE_TELEMETRY=1     # optional: no anonymous install telemetry
curl -fsSL https://www.nvidia.com/nemoclaw.sh | bash
```

## 8.5 Create the two sandboxes

`scripts/setup_nemoclaw.sh` runs all of this for both sandboxes.

**1. Onboard Hermes with Token Factory and Telegram**
```bash
NEMOCLAW_PROVIDER=custom \
NEMOCLAW_ENDPOINT_URL=https://api.tokenfactory.nebius.com/v1 \
NEMOCLAW_MODEL=<Nemotron 3.5 Lightning model ID> \
COMPATIBLE_API_KEY=$NEBIUS_API_KEY \
TELEGRAM_BOT_TOKEN=$DEALER_BOT_TOKEN \
  nemohermes onboard --name dealer-desk --non-interactive --yes-i-accept-third-party-software
```
Onboarding checks the endpoint, tool calling and streaming. If Telegram is
not on after onboarding:
```bash
NEMOCLAW_NON_INTERACTIVE=1 TELEGRAM_BOT_TOKEN=$DEALER_BOT_TOKEN nemohermes dealer-desk channels add telegram
nemohermes dealer-desk rebuild
```

**2. Who may talk to each bot**
- **shop-desk:** `TELEGRAM_ALLOWED_IDS=<owner id>,<packer ids>`. Nobody else can use the shop bot.
- **dealer-desk:** no allowlist. Hermes then answers each new sender with an
  8-character pairing code (Hermes default `unauthorized_dm_behavior: pair`).
  Approve a dealer's code from the host:
  ```bash
  nemohermes dealer-desk exec -- hermes pairing approve telegram <CODE>
  nemohermes dealer-desk exec -- hermes pairing list
  ```
  This is a first gate. The second gate is the shop's own dealer approval
  (`APPROVE DEALER <id>` on the shop bot).

**3. Connect the MCP server**
```bash
export DEALER_MCP_TOKEN=...      # the same value as in .env
nemohermes dealer-desk mcp add order-desk --url https://<tunnel>/dealer/mcp --env DEALER_MCP_TOKEN
nemohermes dealer-desk mcp status order-desk
```
NemoClaw stores the token outside the sandbox and adds it at the network
boundary. The agent never sees it. Do the same for `shop-desk` with
`/shop/mcp` and `SHOP_MCP_TOKEN`.

**4. Add the instructions**
```bash
nemohermes dealer-desk skill install hermes/dealer-desk/skills/dealer-desk
nemohermes dealer-desk upload hermes/dealer-desk/SOUL.md /sandbox/     # NemoClaw keeps SOUL.md at /sandbox/SOUL.md
nemohermes dealer-desk gateway restart
```

## 8.6 How a photo reaches the MCP server

Hermes downloads a Telegram photo into its media cache inside the sandbox
(`/sandbox/.hermes/image_cache/...`) and adds the line
`[Image attached at: <path>]` to the message (Hermes v2026.5.29 and later).
The agent passes `image_ref = "sandbox:<path>"`. The MCP server runs on the
host, so it copies the file out with NemoClaw's own command:

```bash
nemohermes dealer-desk download /sandbox/.hermes/image_cache/<file> <temp dir>/
```

No token goes into the sandbox, and no extra network policy is needed. The
server copies only files under the Hermes media cache folders, never other
sandbox files. Set `DEALER_SANDBOX` / `SHOP_SANDBOX` in `.env` if you use
other sandbox names.

**Caller identity.** The Hermes gateway writes the sender's user ID into the
"Current Session Context" part of the system prompt. Keep the Hermes setting
`privacy.redact_pii` at its default (`false`); if it is `true`, IDs are
replaced by hashes and the tools cannot find the dealer.

**One check on the real bot.** Send a photo and ask the bot: "Which path and
user ID do you see for my last message?" You should see an `image_cache`
path and your numeric Telegram ID. If your Hermes version shows something
else, use the reference runtime (8.7).

## 8.7 Fallback: the reference runtime

```bash
nemohermes dealer-desk stop          # one reader per bot token
order-desk devbot --role dealer
# later: nemohermes dealer-desk start
```
The reference agent uses the same tools, instructions and Nemotron model on
Token Factory. It takes the caller ID and the photo's `tg:` file ID from the
Telegram update in code. The shop bot can keep running in its sandbox.

## 8.8 Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `401 unauthorized` from the server | Token in the sandbox differs from `.env` | `mcp add` again with the right variable |
| `mcp add` refuses the URL | `http://`, `localhost`, or a private address without trust | Use the HTTPS tunnel address |
| Messages do not arrive | The outbox sender is not running | `make local`; check `data/logs/sender.log` |
| `read_chitti_photo` says it could not copy the photo | Wrong sandbox name, or `nemohermes` not on the server's PATH | Set `DEALER_SANDBOX`, `NEMOHERMES_BIN` in `.env` |
| A new dealer gets a code, not a reply | Hermes pairing | `nemohermes dealer-desk exec -- hermes pairing approve telegram <CODE>` |
| `external_service_error` | Wrong model ID or key | Copy the exact model ID from the Token Factory console |
| The bot answers twice or not at all | Hermes and `devbot` use the same bot token | Run only one of them per bot |

## 8.9 Sources

- NemoClaw home: https://docs.nvidia.com/nemoclaw/latest/user-guide/openclaw/home
- Platform support: https://docs.nvidia.com/nemoclaw/latest/user-guide/hermes/reference/platform-support
- OpenAI-compatible endpoint: https://docs.nvidia.com/nemoclaw/latest/user-guide/hermes/inference/custom-endpoints/set-up-openai-compatible-endpoint
- Add an MCP server: https://docs.nvidia.com/nemoclaw/latest/user-guide/hermes/manage-sandboxes/mcp-servers/add-an-mcp-server
- Telegram: https://docs.nvidia.com/nemoclaw/latest/user-guide/hermes/manage-sandboxes/messaging-channels/set-up-telegram
- Run several sandboxes: https://docs.nvidia.com/nemoclaw/latest/user-guide/hermes/manage-sandboxes/operate-sandboxes/run-sandboxes
- Add channels after onboarding: https://docs.nvidia.com/nemoclaw/latest/user-guide/hermes/manage-sandboxes/messaging-channels/add-channels-after-onboarding
- Manage channels (stop, start, remove): https://docs.nvidia.com/nemoclaw/latest/user-guide/hermes/manage-sandboxes/messaging-channels/manage-messaging-channels
- Skills, exec, gateway restart: https://docs.nvidia.com/nemoclaw/latest/user-guide/hermes/manage-sandboxes/install-hermes-plugins
- Upload / download files, SOUL.md location: https://docs.nvidia.com/nemoclaw/latest/user-guide/hermes/manage-sandboxes/state-and-backups/transfer-state-manually and .../understand-sandbox-state
- Hermes pairing and allowlists: https://hermes-agent.nousresearch.com/docs/user-guide/security
- Hermes session context and ID redaction: https://hermes-agent.nousresearch.com/docs/developer-guide/gateway-session-lifecycle and https://github.com/NousResearch/hermes-agent/pull/1542
- Hermes Telegram image path hint: https://github.com/NousResearch/hermes-agent/issues/20899 and /issues/19287
