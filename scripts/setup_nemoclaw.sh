#!/usr/bin/env bash
# =============================================================================
# Set up the two NemoClaw + Hermes sandboxes for the Order Desk.
#
#   dealer-desk : Hermes + DEALER bot + /dealer/mcp (dealer tools only)
#   shop-desk   : Hermes + SHOP bot   + /shop/mcp   (owner and packer tools)
#
# Before you run this:
#   1. NemoClaw is installed and Colima or Docker Desktop is running.
#   2. .env is filled in (README step 2), including MCP_PUBLIC_URL.
#   3. The MCP server runs (`make local`) behind an HTTPS tunnel.
#
# Every command comes from the NemoClaw and Hermes docs. Sources are listed in
# docs/08-nemoclaw-setup.md, section 8.9.
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a

: "${MCP_PUBLIC_URL:?Set MCP_PUBLIC_URL in .env, for example https://abc.trycloudflare.com}"
: "${NEBIUS_API_KEY:?}" "${DEALER_BOT_TOKEN:?}" "${SHOP_BOT_TOKEN:?}"
: "${DEALER_MCP_TOKEN:?}" "${SHOP_MCP_TOKEN:?}" "${OWNER_TELEGRAM_IDS:?}"
DEALER_SANDBOX="${DEALER_SANDBOX:-dealer-desk}"
SHOP_SANDBOX="${SHOP_SANDBOX:-shop-desk}"

onboard() {   # $1 sandbox, $2 bot token, $3 allowed Telegram IDs ("" = pairing for every new sender)
  echo "=== 1/4 Onboard $1: Hermes + Token Factory (OpenAI-compatible) + Telegram"
  NEMOCLAW_PROVIDER=custom \
  NEMOCLAW_ENDPOINT_URL="${NEBIUS_BASE_URL%/}" \
  NEMOCLAW_MODEL="$AGENT_MODEL" \
  COMPATIBLE_API_KEY="$NEBIUS_API_KEY" \
  TELEGRAM_BOT_TOKEN="$2" \
  TELEGRAM_ALLOWED_IDS="$3" \
  TELEGRAM_REQUIRE_MENTION=1 \
    nemohermes onboard --name "$1" --non-interactive --yes-i-accept-third-party-software
  # If Telegram is not on after onboarding, add it and rebuild:
  #   NEMOCLAW_NON_INTERACTIVE=1 TELEGRAM_BOT_TOKEN="$2" nemohermes "$1" channels add telegram
  #   nemohermes "$1" rebuild
}

add_mcp() {   # $1 sandbox, $2 path (dealer|shop), $3 name of the token variable
  echo "=== 2/4 Connect $1 to ${MCP_PUBLIC_URL%/}/$2/mcp"
  nemohermes "$1" mcp add order-desk --url "${MCP_PUBLIC_URL%/}/$2/mcp" --env "$3"
  nemohermes "$1" mcp status order-desk
}

instructions() {   # $1 sandbox, $2 role folder (dealer-desk|shop-desk)
  echo "=== 3/4 Instructions for $1: skill + SOUL.md"
  nemohermes "$1" skill install "hermes/$2/skills/$2"
  # NemoClaw keeps the Hermes persona file at /sandbox/SOUL.md.
  nemohermes "$1" upload "hermes/$2/SOUL.md" /sandbox/
  nemohermes "$1" gateway restart
}

onboard "$DEALER_SANDBOX" "$DEALER_BOT_TOKEN" ""
add_mcp "$DEALER_SANDBOX" dealer DEALER_MCP_TOKEN
instructions "$DEALER_SANDBOX" dealer-desk

onboard "$SHOP_SANDBOX" "$SHOP_BOT_TOKEN" "${OWNER_TELEGRAM_IDS}${PACKER_TELEGRAM_IDS:+,$PACKER_TELEGRAM_IDS}"
add_mcp "$SHOP_SANDBOX" shop SHOP_MCP_TOKEN
instructions "$SHOP_SANDBOX" shop-desk

cat <<NEXT

=== 4/4 Done. How new dealers get access
The dealer bot has no allowlist, so Hermes answers a new sender with an
8-character pairing code. When a dealer sends you the code, approve it:

    nemohermes $DEALER_SANDBOX exec -- hermes pairing approve telegram <CODE>
    nemohermes $DEALER_SANDBOX exec -- hermes pairing list

After pairing, the bot itself asks the dealer to register, and you approve the
dealer on the shop bot with: APPROVE DEALER <telegram id>.

Test: send /start to the dealer bot, then a chitti photo.
NEXT
