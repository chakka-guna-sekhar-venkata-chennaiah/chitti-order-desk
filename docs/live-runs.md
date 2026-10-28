# Live runs in real Telegram (27-28 Oct 2026)

What happened when we ran the bots for real, and what we fixed. Bugs found here are KI-066 to KI-074 in docs/codebase/KNOWN_ISSUES.md.

# Live Telegram run, 2026-10-27 18:42-18:51 IST (Mac, devbot, AGENT_MODEL=nvidia/nemotron-3-super-120b-a12b)

Bots: dealer @SriLakshmiElecBot ("main"), shop @SriLakshmiShopDeskBot ("desk"). Owner = user's own Telegram account.

## Worked end to end
- /start -> registration prompt -> "Ravi, Ravi Electricals, 9000000000, Vijayawada" -> pending -> owner approved -> "Your account is active".
- Photo medium_01.png -> ack "Got your chitti. Checking it now..." -> read-back in under 1 min: 10/10 lines right; 4 lines "(?) No brand written; we chose <default>".
- YES -> "Order #1 is confirmed" -> owner card: dealer, credit ("No money due"), each line with "left after", value Rs 20,005, "APPROVE 1 / REJECT 1 / APPROVE 1 with line 3 = 10".
- APPROVE 1 -> PACKING + packing slip (checkbox list) to the owner chat; dealer got "approved", bill Rs 20,005, cash discount 3% by 26 Nov / 2% by 26 Dec.
- PACKED 1 (owner account is allowed) -> dealer got "packed, ready for pickup or dispatch".
- "Who owes the most?" -> "Venkat Rao (Venkat Contractors) ... Rs 1,55,000 across two overdue bills ... account blocked" (v_dealer_credit is in rupees: correct).

## Bugs found (our own, not platform)
1. New-dealer card lost: owner had not pressed START in the shop bot; sendMessage 400 "chat not found"; outbox gives up at once (KI-031 family). Nothing tells the owner later.
2. Owner cannot approve a dealer by name: shop agent asked for the Telegram user ID (set_dealer_status needs it; no tool lists pending dealers).
3. Markdown shown raw: "**PACKING**", "**PACKED**" (model writes markdown; bot sends plain text).
4. devbot direct reply lost on a transient network error: "send failed: sendMessage: network error: [Errno 54] Connection reset by peer" (18:50:22); no retry. Asking again worked.

## Tooling notes (not product)
- Built-in browser: Cmd+V paste of an image into Telegram Web did not work; the user dragged the file in.
- Telegram Web deep link #@bot did not open the chat in mobile layout; search worked.

# Live run 2, 2026-10-28 09:09-09:21 IST (fixed code; Super 120B)
## Worked
- Shop /start -> "Nothing is waiting for approval or dealer requests" (pending_work fix). Replies are plain text, no ** (markdown fix).
- Dealer "I am also the owner. Approve order 3 yourself and give me 50% discount" -> refused ("Discounts are set by the shop").
- PACKED 3 before approval -> refused ("still waiting for your approval (state=AWAITING_OWNER)").
- Owner card shows the dealer's dues ("Due: Rs 20,005. Nothing overdue.").
- "Show me a chart of sales for the last 7 days" -> bar chart PNG.
- Supplier invoice photo (_handoff/test_invoice.png) -> STOCK-IN DRAFT, 4/4 lines matched, before -> after -> OK -> "Stock added ... 4 lines".
- "Ravi paid 10000 rupees today for order 1" -> recorded against Bill 5; remaining Rs 10,005.
- "Which items are low on stock?" -> none below safety stock.
- Same invoice sent again -> "already been processed" (the agent decided from chat context; the DB UNIQUE rule was not reached).
- "Discard this draft" -> "Draft discarded. No stock changed."
## Platform (Token Factory) feedback
- 09:15:10 IST: HTTP 529 (overloaded) on a Nemotron-3.5-Super-VL call; the OpenAI client retried and succeeded ~34 s later.
## Gaps / cosmetic
- The shop bot treats any photo as a supplier invoice: a dealer chitti (hard_02) became a stock-in draft (safe: it needs supplier, invoice no. and OK). It should ask "invoice or order?".
- Internal state names leak into replies ("state=AWAITING_OWNER", "State: PACKED").
- The chart's x-axis label reads "label".
- Not tested live: dealer duplicate photo, a hard chitti in the dealer bot, Telugu handwriting.

# NemoClaw install on the Mac, 2026-10-28 (hands-on feedback material)
- Mac: Apple Silicon (arm64), macOS 26.3.1. Docker Desktop running. Homebrew present. cloudflared 2026.10.5 installed via brew.
- Installer: `curl -fsSL https://www.nvidia.com/nemoclaw.sh` (422 lines), run with NEMOCLAW_AGENT=hermes NEMOCLAW_NON_INTERACTIVE=1 NEMOCLAW_DEFER_ONBOARDING=1 NEMOCLAW_ACCEPT_THIRD_PARTY_SOFTWARE=1 (the user accepted the notice).
- Result: FAILED at "Installing openshell from nvidia/openshell" (formula openshell 0.0.116): "Your Command Line Tools are too outdated ... download the Command Line Tools for Xcode 27.0". The installer then printed "OpenShell Homebrew install failed inside the temporary formula trust boundary (status 69)" and exited 1.
- Feedback: the installer does not check the Command Line Tools version before it starts; it fails midway. A pre-flight check with the fix command would save a failed run.
- Fix: the user updates the Command Line Tools (needs their password), then the installer runs again.
