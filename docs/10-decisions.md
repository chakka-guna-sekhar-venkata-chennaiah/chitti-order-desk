# 10. Decisions, limits and roadmap

Each decision gives the choice, the reason, and the cost.

| # | Decision | Reason | Cost |
|---|---|---|---|
| 1 | **SQLite is the CRM.** No external CRM. | The target shops have no CRM. One file is easy to run on a shop PC and to back up. | One writer at a time. Enough for one shop; a chain needs Postgres. |
| 2 | **The model reads, the code decides, the owner approves.** | Stock, money and states need exact, testable rules. | More tools to build. |
| 3 | **Two bots, two sandboxes, two token scopes.** | Permission by construction: the dealer agent cannot see owner tools. | Two sandboxes to run. |
| 4 | **Deterministic matcher** instead of a model for SKU matching. | Free, fast, 100 % testable, explainable notes ("No brand written; we chose Finolex"). | New products need aliases and specs. |
| 5 | **Read-back before confirm**, with "?" on doubtful lines. | The reading is the single point of failure. The dealer is the best checker. | One more message for the dealer. |
| 6 | **One approval card per order**, with stock and credit on it. | The owner decides once, with all facts. | |
| 7 | **Credit is shown, not enforced** (except "blocked"). | Credit decisions are the owner's. | |
| 8 | **Append-only stock movements.** | History and audit; a mistake is corrected with a new row. | Stock is a SUM (fast enough with an index). |
| 9 | **Money in paise, time in UTC text.** | No rounding errors; readable for text-to-SQL. | |
| 10 | **Transactional outbox.** | No lost messages after a crash. | At-least-once: a rare duplicate message. |
| 11 | **Stateless MCP over HTTPS.** | Works behind a tunnel; no session to lose. | |
| 12 | **Image by reference (`tg:` / `up:`), not in the tool call.** | NemoClaw's 128 KB MCP request limit; models must not copy base64. | The server needs the bot token to download. |
| 13 | **Reference agent next to Hermes.** | Reproducible evals and a fallback runtime. | Two runtimes; one instruction source keeps them equal. |
| 14 | **Evals check with code.** | Structured outputs make exact checks possible and cheap. | The dealer simulator is still a model. |

## Limits today

- The 15 golden chittis are synthetic, because shops did not share real ones yet. Real chittis may score lower.
- Telugu-script lines are not tested.
- WhatsApp: NemoClaw pairs WhatsApp Web by QR code. There is no WhatsApp
  Business API path in NemoClaw today, so the demo uses Telegram.
- Hermes: NemoClaw tests Hermes, but does not claim production parity with
  OpenClaw.
- Caller identity under Hermes comes from the model ([09-security.md](09-security.md)).
- PDFs of invoices are not read; send a photo or a screenshot.
- No payments collection, no delivery tracking, no GST invoice.

## Roadmap

1. Show the demo to shop owners; collect real chittis (names removed) for the golden set; add Telugu lines.
2. Verified sender ID passed to the MCP server outside the model.
3. On-premise model: NemoClaw routes the sandboxes to a local server
   (Ollama, vLLM or NIM) on a shop PC or a DGX Spark; the tools and the
   database do not change.
4. WhatsApp Business API channel.
5. Tally / Vyapar export of approved bills.
6. Stock-in from PDF invoices.
7. Postgres for multi-branch shops (advisory locks replace BEGIN IMMEDIATE).
