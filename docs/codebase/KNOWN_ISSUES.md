# Known issues

> Verified against code on 2026-10-27 · Scope: confirmed and suspected bugs, security gaps, reliability risks, tech debt, environment problems, doc drift and open questions found while writing the six codebase docs · Related: [CODEBASE_MAP](CODEBASE_MAP.md), [RUNTIME_AND_OPERATIONS](RUNTIME_AND_OPERATIONS.md), [DOMAIN_AND_DATA_MODEL](DOMAIN_AND_DATA_MODEL.md), [AGENT_MCP_AND_VISION](AGENT_MCP_AND_VISION.md), [DATA_FLOW](DATA_FLOW.md), [CHANGE_GUIDE](CHANGE_GUIDE.md)

How to read this file
- Nothing here was fixed or run by the people who wrote it. "Found by: reading" means a static read of the code; no test or eval was executed (the suite could not run on this Mac then, see KI-055; it was run on 2026-10-27: 84 passed before the KI-001 fix, 86 after). The only executed checks are the environment probe (KI-055) and an in-memory copy of the SQL authorizer (KI-024).
- The P1 bugs and the Security items were re-read against the code on 2026-10-27. For the other items the cited line numbers were spot-checked with grep (they matched); the rest of their detail comes from the 2026-10-26 code reading.
- IDs are stable and never reused. Add a new issue with the next free number at the end of its group. When you fix one, change its line to `Status: fixed (date, change)` and update the doc that described the old behaviour.
- P1 = breaks a documented flow or loses data, with high confidence. P2 = real but narrower or lower impact.

## 1. P1 bugs

### KI-001 Re-confirm after `reopen_order` fails with `internal_error`
- **Evidence (before the fix; line numbers have moved):** `src/order_desk/db/schema.sql:119` `UNIQUE (order_id, sku)` on `reservations` also covers `released` rows. `src/order_desk/services/stock.py:82-88` `release()` only sets `status='released'`; `:75-79` `reserve()` INSERTs again. Path: `src/order_desk/services/dealer_desk.py:185-198` `reopen()` (release at `:190`), then `:159-183` `confirm()` (reserve at `:178`).
- **Impact (before the fix):** after a reopen, `confirm_order` returned `internal_error` whenever any previously reserved SKU was still on the order (`sqlite3.IntegrityError` is mapped by `src/order_desk/mcp_server/tools.py` `_safe()`). The dealer could only cancel or wait for expiry. `docs/04-order-states.md` and `src/order_desk/agent/instructions/dealer.md:72` tell dealers to reopen and confirm again; the approval key `approval:{id}:{version}` shows re-confirm was meant to work.
- **Found by:** reading; reproduced on 2026-10-27 (both new tests failed with `sqlite3.IntegrityError: UNIQUE constraint failed: reservations.order_id, reservations.sku` at `stock.py:76`) · Status: fixed (2026-10-27, `stock.reserve()` makes the order's own `released` row `active` again; see Fix)
- **Fix:** `src/order_desk/services/stock.py` `reserve()` first runs `UPDATE reservations SET qty, status='active', created_at, closed_at=NULL WHERE order_id AND sku AND status='released'` and INSERTs only when no row was updated. `release()` is unchanged (the rows stay `released`, the history the docs describe). No schema change, so existing databases need no migration (KI-043). A second `active` or `consumed` row for the same order and SKU still raises `IntegrityError`; the state machine cannot produce one. Tests in `tests/test_order_flow.py`: `test_confirm_again_after_reopen_holds_the_stock_again` (confirm, reopen, confirm, approve; reserved 10 then on-hand down by 10; the owner gets a second approval card) and `test_confirm_again_after_reopen_holds_only_the_lines_that_are_left` (smaller quantity, removed SKU stays free, new SKU, second round). The `deps` fixture checked the invariants at the end of both. Suite: 84 passed before, 86 after.

### KI-002 A discarded supplier invoice cannot be read or entered again
- **Evidence:** `src/order_desk/db/schema.sql:219` `UNIQUE (supplier, invoice_no)` counts discarded drafts. `src/order_desk/services/shop_desk.py:520-526` `_refuse_known_invoice()` skips `state = 'discarded'` (and compares the supplier case-insensitively), so the INSERT in `read_supplier_invoice` (`:288-293`) and the UPDATE in `update_stock_in` (`:313`) hit the UNIQUE.
- **Impact:** `internal_error` for the owner, after a paid vision call (the model runs before the check, `:277-298`). The owner has no way to re-enter the same invoice. Only for an exact-case supplier match.
- **Found by:** reading · **Fix direction:** a partial unique index `WHERE state <> 'discarded'` (needs a migration path, KI-043), or reuse the discarded row; run the duplicate check before the model call · Status: open

### KI-003 A shelf count below the reserved quantity breaks the stock rules
- **Evidence:** `src/order_desk/services/shop_desk.py:231-243` `adjust_stock()` sets on-hand to the counted number without looking at active reservations. `src/order_desk/services/stock.py:106` `deduct_for_order()` counts the order's own reservation as available (`sellable + held`), so a later approval can push on-hand below zero. `src/order_desk/services/invariants.py` rule 1 then fails.
- **Impact:** negative or over-reserved stock after a count lower than what is promised to waiting orders. The invariants run only in tests and evals, never in production, so nothing flags it.
- **Found by:** reading · **Fix direction:** refuse or warn when `counted_qty` is below active reservations, or re-check on-hand at approval; add a test · Status: open

### KI-004 `order-desk demo --workdir X` deletes X
- **Evidence:** `src/order_desk/demo.py:82-83` `run_demo()` calls `shutil.rmtree(workdir)` when it exists; `src/order_desk/cli.py:89` accepts any `--workdir` (default `data/demo`). No marker file, no confirmation.
- **Impact:** `order-desk demo --workdir data` (or `.`) deletes the real database, uploads and logs, or the whole working tree. This folder is not under git, so nothing can restore it (KI-056).
- **Found by:** reading · **Fix direction:** delete only a directory that holds a marker file the demo wrote, or refuse a non-empty directory; add a confirmation · Status: open

## 2. P2 bugs

### KI-005 A resent photo is cancelled as a duplicate of a stuck read
- **Evidence:** `src/order_desk/services/dealer_desk.py:125` stores `image_sha256` before the model call (`:127`). If the call fails the order stays RECEIVED. `_same_file_recently()` (`:241-248`) does not exclude RECEIVED, so the same bytes sent again become an order that is CANCELLED as a duplicate (`:117-123`), and `dealer.md` then says to stop.
- **Impact:** the dealer is stuck until the agent re-reads the old order. `src/order_desk/services/maintenance.py` `report_stuck_reads()` and `docs/02-data-flow.md` §2.1 name "the dealer resends" as the recovery. Whether a resent photo has identical bytes is unknown.
- **Found by:** reading · **Fix direction:** ignore RECEIVED orders without lines in the duplicate check, or clear the hash when the read fails · Status: open

### KI-006 Several tools are not safe to repeat, although the docs say they are
- **Evidence:** `read_chitti_photo(append=true)` appends the lines again (`src/order_desk/services/dealer_desk.py:231-234`, `:117`, `src/order_desk/services/orders.py:121-124`). `update_order_lines` with `add_sku` and `update_stock_in` with `add_sku` add a duplicate line. `read_supplier_invoice` without supplier and number creates a new draft per retry (`src/order_desk/services/shop_desk.py:285-294`). `add_product`, `reopen_order` and `discard_stock_in` error on a second call. `send_chart`: KI-015.
- **Impact:** wrong quantities reach the read-back and the dealer must notice. See DD-04 and DD-05.
- **Found by:** reading · **Fix direction:** store the hash of appended images and skip repeats; give add operations an operation key · Status: open

### KI-007 Duplicate SKU lines make `confirm_order` fail without a read-back
- **Evidence:** `src/order_desk/services/orders.py:138-154` `apply_stock_check()` compares each line to sellable on its own; `:53-59` `active_items()` sums per SKU; `src/order_desk/services/stock.py:63-73` `reserve()` then raises `NotEnoughStock` for the sum and the whole confirm rolls back.
- **Impact:** the dealer gets `not_enough_stock` with no read-back and no line marked short, so the agent cannot explain what to change.
- **Found by:** reading · **Fix direction:** run the stock check on the per-SKU sum, or merge duplicate lines · Status: open

### KI-008 `record_payment` idempotency is global per reference, with no settled-bill guard
- **Evidence:** `src/order_desk/services/shop_desk.py:218` key `payment:{payment_ref}` is not scoped to a bill; `src/order_desk/services/credit.py:86-104` has no check for an already settled bill or an overpayment; `src/order_desk/agent/instructions/shop.md:53-55` suggests the Telegram message ID as the reference (unique only per chat).
- **Impact:** the same reference used on another bill returns the first result (`repeat: true`) and records nothing; a second payment on a settled bill is accepted and the paid date and discount are recomputed.
- **Found by:** reading · **Fix direction:** key on bill plus reference; refuse settled bills and overpayment · Status: open

### KI-009 Reader doubt signals default to "no doubt"
- **Evidence:** `src/order_desk/domain/chitti.py:115-116` maps a missing or unknown `mark`/`legibility` to `none`/`clear`; `:106-107` the category must match exactly (case-sensitive); `src/order_desk/domain/catalog_terms.py:49-55` `COLOR_CODES` are never applied, so a color `r` finds no match and the line is `unmatched`. Tested as intended in `tests/test_matching.py` `test_model_json_with_fences_and_bad_values_is_cleaned`.
- **Impact:** a model that leaves out these fields makes lines look certain. The matcher still needs category, spec and brand, so the damage is bounded.
- **Found by:** reading · **Fix direction:** default to the doubtful value; normalise category case; apply or delete `COLOR_CODES` · Status: open

### KI-010 The business day is UTC, and one view ignores the injected clock
- **Evidence:** `src/order_desk/services/reports.py:57` `today_summary()`, `src/order_desk/db/schema.sql:321` `created_date` and `:367` `sale_date` use UTC dates (the catalog dedupe hour and PDF date too). `schema.sql:356-359` `v_dealer_credit` uses `strftime(..., 'now')`, not `src/order_desk/clock.py`.
- **Impact:** "today" flips at 05:30 IST. Under a frozen test clock `src/order_desk/services/credit.py` `summary()` and text-to-SQL answers about overdue bills can disagree.
- **Found by:** reading · **Fix direction:** a shop time-zone setting; compute overdue in code or pass the date to the view · Status: open

### KI-011 New multi-brand products never get a default brand
- **Evidence:** `src/order_desk/services/shop_desk.py:245-264` `add_product()` always stores `is_default_brand = 0` and no tool changes it. `src/order_desk/services/matching.py:150-152` then answers an unbranded line "Please give the brand."; `:154` says "No brand written" even when a brand was written but several same-brand products remain.
- **Impact:** a product group added by the owner can never auto-match an unbranded line; the prompt can be misleading.
- **Found by:** reading · **Fix direction:** let `add_product` set the default brand; fix the message · Status: open

### KI-012 Stock-in edits: stale `sell_units`, and the model runs before the duplicate check
- **Evidence:** `src/order_desk/services/shop_desk.py:332-338` `update_stock_in()` changing only `sku` keeps `sell_units` computed for the old product's pack size; `:277-298` `read_supplier_invoice` calls the model before any duplicate check and an empty reading creates an empty draft.
- **Impact:** wrong quantity if the owner corrects a SKU; a paid call is wasted on a known invoice.
- **Found by:** reading · **Fix direction:** recompute `sell_units` on a SKU change; check duplicates first (see KI-002) · Status: open

### KI-013 Prices can be stale or differ between surfaces
- **Evidence:** `src/order_desk/services/shop_desk.py:60-61` `order_details()` totals use current prices even after approval (not `order_lines.unit_price_paise` or the bill); `src/order_desk/services/dealer_desk.py:345` `search_products()` shows base prices while `src/order_desk/documents/catalog_pdf.py:39-43` shows the dealer's price.
- **Impact:** owner and dealer can see different numbers for one item.
- **Found by:** reading · **Fix direction:** use the stored prices after approval; use the dealer price in search · Status: open

### KI-014 A short "assumed" line that becomes sellable loses its note
- **Evidence:** `src/order_desk/services/orders.py:150-154` `apply_stock_check()` turns it `ok` and drops the "we chose <brand>" note and the `(?)` marker.
- **Impact:** the dealer is no longer told which brand was assumed.
- **Found by:** reading · **Fix direction:** keep the `assumed` status when stock returns · Status: open

### KI-015 The chart dedupe key is a timestamped file name
- **Evidence:** `src/order_desk/services/shop_desk.py:428-429` key `chart:<filename>`; `src/order_desk/documents/charts.py:51` builds the name from the clock.
- **Impact:** a retried call sends a second chart; under a frozen test clock two charts in one test collide and the second is dropped.
- **Found by:** reading · **Fix direction:** key on staff ID, chart type and date · Status: open

### KI-016 The scheduler counts reminders that were suppressed
- **Evidence:** `src/order_desk/services/maintenance.py:49-55` `remind_owner()` returns the number of waiting orders even when the dedupe key skipped the insert, so `src/order_desk/workers/scheduler.py:29-30` logs `scheduler: {...}` every minute while an order waits. The bucket key `reminder:{id}:{bucket}` restarts at 1 after a reopen and re-confirm.
- **Impact:** noisy logs that hide real events.
- **Found by:** reading · **Fix direction:** count rows actually inserted · Status: open

### KI-017 Two concurrent `read_chitti_photo` calls both pay for the model
- **Evidence:** `src/order_desk/services/dealer_desk.py:115-126` the middle transaction of `read_chit()` does not re-check the state. The loser fails safely in `_save_reading()` with `conflict` or `illegal_transition`, and its rollback also undoes its `DELETE FROM order_lines`.
- **Impact:** cost only.
- **Found by:** reading · **Fix direction:** mark the order as being read (a lease) before the call · Status: open

### KI-018 Malformed model output and undecodable images become `internal_error` and leave the order stuck
- **Evidence:** `src/order_desk/vision/reader.py:95` `prepare_image()` runs before the try at `:101-107`, so PIL errors (corrupt file, RIFF that is not WEBP, decompression bomb) are not wrapped. `src/order_desk/domain/chitti.py:69-70` (`lines` not a list raises `ValueError`) and `:102-104` (`item` or `spec` not a dict raises `AttributeError`) escape; the JSON re-ask in `reader._ask()` covers only `_json_object()` failures. `src/order_desk/vision/images.py:116-127` `_from_telegram()` reports a missing bot token as a retryable `ExternalServiceError`. `src/order_desk/services/maintenance.py:58-68` `report_stuck_reads()` only logs.
- **Impact:** the dealer gets `internal_error` instead of a retry hint and the order stays RECEIVED (see KI-005).
- **Found by:** reading · **Fix direction:** wrap decode and parse errors in `ExternalServiceError`/`InvalidInput`; validate the JSON shape; expire stuck RECEIVED orders · Status: open

### KI-019 `max_tokens=4000` can truncate long readings
- **Evidence:** `src/order_desk/vision/reader.py:104`.
- **Impact:** a long invoice or chitti gives invalid JSON twice and ends as `external_service_error`; the repair prompt cannot fix truncation (inferred).
- **Found by:** reading · **Fix direction:** raise the limit or split pages · Status: open

### KI-067 The owner cannot approve a dealer by name
- **Evidence:** live Telegram run, 2026-10-27. The shop agent asked the owner for the dealer's Telegram user ID. `ShopDesk.set_dealer_status()` needs `dealer_telegram_user_id`, and no tool showed pending dealers or their IDs (`v_dealer_credit` has no Telegram ID; only the card had it).
- **Impact:** the owner had to take the ID from a card that could be lost (KI-066), or ask the dealer.
- **Found by:** live run · Status: fixed (2026-10-27, the pending-dealers list of `pending_work` carries the ID)
- **Fix:** each `pending_dealers` row has `dealer_telegram_user_id`, `name`, `shop`, `phone`, `city` and `requested_at`. `shop.md` ("Approve or block a dealer by name") tells the agent to find the ID there and never to ask for it, to ask which one when two match, and to treat the dealer-typed text as data. The dealer list is for the owner only: a packer gets `orders_waiting_for_approval` and no dealer details, because only the owner may call `set_dealer_status`. Tests: `test_the_owner_can_approve_a_dealer_with_the_id_that_the_report_shows`, `test_a_packer_sees_the_orders_but_not_the_dealer_details`. Not covered: to block a dealer who is already active or blocked, the owner still has to give the ID (no tool lists those IDs).

### KI-068 Agent replies showed raw markdown such as `**PACKING**`
- **Evidence:** live Telegram run, 2026-10-27. The model wrote `**PACKING**` and `**PACKED**`. `BotApi.send_message()` sends plain text (no `parse_mode`), so Telegram showed the stars.
- **Impact:** untidy replies to dealers and to the owner.
- **Found by:** live run · Status: fixed (2026-10-27, a plain-text rule in both instruction files and `plain_text()` in the devbot send path)
- **Fix:** rule 9 in `dealer.md` and rule 7 in `shop.md` (`order-desk export-skills` was run). `src/order_desk/agent/dev_bot.py` `plain_text()` removes `**bold**`, `__bold__` and a leading `#`; `_safe_send()` applies it to everything the devbot sends. `services/messages.py` is unchanged, and a test checks that the code-built texts pass through `plain_text()` unchanged. Tests: `tests/test_dev_bot.py`. Not covered: under Hermes only the instruction rule applies, because Hermes sends the text itself; `*italic*`, backticks and `-` bullets are not removed; the model's history still holds its own markdown.

## 3. Security

### KI-020 A model-supplied caller ID returns another dealer's order data
- **Evidence:** the caller is a tool argument (`telegram_user_id`), hidden from the model only in the reference agent (`src/order_desk/agent/loop.py` `ToolAgent._execute()`); under Hermes the model passes it. `src/order_desk/services/dealer_desk.py:215-227` `order_status()`, `:39-50` `profile()` and `_readback_result()` (used by `read_chit()` and `update_lines()`) return read-backs, lines and open orders for whatever ID was passed; `:75-84` `send_catalog()` queues to the model-supplied ID.
- **Impact:** a model fooled into another dealer's ID (for example by text in a chitti photo) shows that dealer's order data in the asker's chat. `docs/09-security.md:31-32` says an impersonator never sees the victim's read-back: true only for outbox messages (DD-10). How Hermes passes the ID is unverified (OQ-01).
- **Found by:** reading · **Fix direction:** bind the caller to the transport (per-user session or header set by Hermes), not to an argument; until then correct `docs/09-security.md` · Status: open

### KI-021 The Telegram bot token is in request URLs and the logs run at INFO
- **Evidence:** `src/order_desk/telegram/bot_api.py:61,78` put the token in the URL path. `logging.basicConfig(level=logging.INFO)` is set in `src/order_desk/workers/outbox_sender.py:68`, `src/order_desk/workers/scheduler.py:21`, `src/order_desk/agent/dev_bot.py:65` and `src/order_desk/mcp_server/server.py:147`; nothing raises the `httpx` logger level.
- **Impact:** `httpx` INFO lines such as `HTTP Request: POST https://api.telegram.org/bot<token>/...` probably land in `data/logs/*.log`, so anyone who can read the logs gets the bot token. Depends on the httpx version; not executed.
- **Found by:** reading · **Fix direction:** set the `httpx` logger to WARNING in one shared logging setup; redact tokens · Status: open

### KI-022 `/uploads` reads the whole body before the size check
- **Evidence:** `src/order_desk/mcp_server/server.py:118` `await request.body()`; the 15 MB limit is only in `src/order_desk/vision/images.py:61` `store()`.
- **Impact:** a holder of the upload token can exhaust memory. Low.
- **Found by:** reading · **Fix direction:** check `Content-Length` and stream with a limit · Status: open

### KI-023 Image sniffing accepts any RIFF file as WEBP
- **Evidence:** `src/order_desk/vision/images.py:38` `_MAGIC` maps `b"RIFF"` to `image/webp` without checking `WEBP` at offset 8.
- **Impact:** other RIFF files (WAV, AVI) reach PIL and fail as `internal_error` (KI-018). `docs/09-security.md` says only JPEG, PNG and WEBP are accepted.
- **Found by:** reading · **Fix direction:** check bytes 8 to 12 · Status: open

### KI-024 The text-to-SQL authorizer allows every name that is not in `sqlite_master`
- **Evidence:** `src/order_desk/services/reports.py:132-133` (`arg1 not in real_tables` is allowed, meant for CTE names); `:118-119` any `;` is refused, even inside a string. In-memory probe on 2026-10-27 (a copy of the authorizer on a database built from `src/order_desk/db/schema.sql`, SQLite 3.51.0 as linked by Python 3.10): `json_each(...)` allowed (harmless); `pragma_table_info`, `pragma_table_list` refused; `dbstat` refused (it reads `sqlite_master`); base tables refused; `sqlite_dbpage` does not exist in that build.
- **Impact:** no data leak found in this build. If the production SQLite is built with the dbpage table, `sqlite_dbpage` would be allowed and would expose raw pages of every table (unverified, OQ-10).
- **Found by:** reading and the in-memory probe · **Fix direction:** deny reads of any `sqlite_*` name and of names that are not CTE aliases of the query; add tests for `sqlite_dbpage`, `json_each`, `pragma_*` in `tests/test_readonly_sql.py` · Status: open

### KI-025 The catalog tools do not identify the caller
- **Evidence:** `src/order_desk/mcp_server/tools.py` `search_catalog` (both scopes) and `send_catalog_pdf` take no identity or status check. `src/order_desk/services/dealer_desk.py:75-84` `send_catalog()` queues the base-price PDF to the model-supplied ID, also for unknown, pending and blocked IDs.
- **Impact:** the price list goes to anyone who can message the bot, and any shop-token holder can use the shop-scope tool. Probably intended for `/start`, but not documented; `docs/05-mcp-tools.md:71` calls shop `search_catalog` "staff" (DD-07).
- **Found by:** reading · **Fix direction:** decide the policy; if prices are private, require a registered, active dealer · Status: open

### KI-026 Staff access is never revoked or downgraded by configuration
- **Evidence:** `src/order_desk/db/seed.py:73-85` `_seed_staff()` inserts or promotes owners (`active = 1`) and uses `DO NOTHING` for packers; nothing under `src/` sets `staff.active = 0` or deletes staff.
- **Impact:** removing an ID from `OWNER_TELEGRAM_IDS` or `PACKER_TELEGRAM_IDS` does not remove access, and an owner moved to the packer list stays owner. Needs manual SQL.
- **Found by:** reading · **Fix direction:** a deactivate command, or make seeding reconcile the list · Status: open

### KI-027 Blocked or pending dealers keep most tools
- **Evidence:** `src/order_desk/services/orders.py:241` `require_dealer_can_order()` is called only from `src/order_desk/services/dealer_desk.py:95` `start_order()`. `read_chit`, `update_lines`, `confirm`, `reopen`, `cancel`, `order_status` and `send_catalog` do not check `dealers.status`.
- **Impact:** a dealer blocked after `start_order` can still confirm, and the approval card reaches the owner. See OQ-13.
- **Found by:** reading · **Fix direction:** check the status in `confirm()` at least · Status: open

### KI-028 Packers can read dealer credit data
- **Evidence:** `src/order_desk/services/shop_desk.py:402-403` `report()` allows `ANY_STAFF`, and its `debtors` report lists credit; `get_order_details` also shows credit and order value to packers (`docs/codebase/AGENT_MCP_AND_VISION.md` §3.4). `get_dealer_bills` is owner-only.
- **Impact:** inconsistent access policy (OQ-19).
- **Found by:** reading · **Fix direction:** make `debtors` owner-only or document the policy · Status: open

### KI-029 Dev-bot traces store personal data in plaintext without a limit
- **Evidence:** `src/order_desk/agent/dev_bot.py:105-106` appends the full tool arguments and results (names and phone numbers from `register_dealer`) to `data/traces/<role>.jsonl`.
- **Impact:** personal data on disk, growing without bound. Reference runtime only.
- **Found by:** reading · **Fix direction:** redact arguments; rotate the file · Status: open

### KI-030 The sandbox network policy is described, not implemented
- **Evidence:** `docs/09-security.md` has a network-policy row; `scripts/setup_nemoclaw.sh` contains no policy setup (no match for "policy").
- **Impact:** do not assume the Hermes sandbox is egress-restricted by anything in this repo (DD-13).
- **Found by:** reading · **Fix direction:** configure it in NemoClaw and script it, or mark the doc "not implemented" · Status: open

## 4. Reliability

### KI-031 The outbox gives up after about five minutes, and failed rows are never retried
- **Evidence:** `src/order_desk/services/outbox.py:29` `MAX_ATTEMPTS = 6`; `:125-136` `mark_failed_attempt()` waits 10, 20, 40, 80 and 160 s, then sets `failed` and writes an `audit_log` row `outbox_gave_up`. No command or tool re-queues a failed row, and no tool or view reads `audit_log`.
- **Impact:** a Telegram or network outage of more than about five minutes on the shop laptop permanently drops approval cards, packing slips and dealer notices, with no alert. `docs/06-reliability.md` §6.3 says "max 10 min" (DD-01).
- **Found by:** reading · **Fix direction:** limit by age instead of count; add `order-desk requeue-failed` and an owner alert for failed rows · Status: open

### KI-032 A wrong token or an unstarted chat fails permanently; the 429 `retry_after` is ignored
- **Evidence:** `src/order_desk/telegram/bot_api.py:86-89` `_call()`: only 429 and status 500 or above are retryable, so 400, 401 and 403 are permanent; the `retry_after` value is never read.
- **Impact:** a wrong or rotated token fails every queued message for that bot on its first attempt; 429 uses the generic backoff. A chat that never pressed START (the owner's, in the live run) loses its cards the same way; the owner can still read the pending work from the database (KI-066).
- **Found by:** reading · **Fix direction:** treat 401 as "pause this bot", keep rows pending; honour `retry_after` · Status: open

### KI-033 With no owner in `staff`, approval cards are silently not queued
- **Evidence:** `src/order_desk/services/outbox.py:77-85` `notify_staff()` queues nothing and returns 0 when no row matches; `src/order_desk/services/dealer_desk.py` `confirm()` still moves the order to AWAITING_OWNER; only `src/order_desk/cli.py:38` prints a warning at `init`.
- **Impact:** orders wait invisibly when `OWNER_TELEGRAM_IDS` was empty at `order-desk init`.
- **Found by:** reading · **Fix direction:** refuse the confirm or alert when no owner exists; reconcile staff at start (KI-026) · Status: open

### KI-034 The worker loops are fragile
- **Evidence:** `src/order_desk/workers/outbox_sender.py:74-77` has no try/except around `claim_due()` and `mark_*()`, so a DB error (for example `database is locked` after the 10 s busy timeout) ends the process. `:71` builds both `BotApi` objects up front and `src/order_desk/telegram/bot_api.py:31-32` raises on an empty token, so a one-bot setup cannot run the sender. `scripts/start_local.sh` does not restart a child that dies.
- **Impact:** queued messages stop without any signal.
- **Found by:** reading · **Fix direction:** catch, log and back off; build bots lazily; supervise with launchd · Status: open

### KI-035 Expiry and reminders only happen while the scheduler runs
- **Evidence:** `src/order_desk/services/maintenance.py:22-37` is the only expiry path; `src/order_desk/services/dealer_desk.py:159-183` `confirm()` does not check `dealer_check_deadline`.
- **Impact:** without `order-desk scheduler` no order expires and the owner gets no reminders; a dealer can confirm after the deadline. `docs/04-order-states.md` says a late reply gets EXPIRED (DD-17).
- **Found by:** reading · **Fix direction:** check the deadline in `confirm()`; document the scheduler as a required process · Status: open

### KI-036 The reference agent's give-up reply promises a human reply that nobody sends
- **Evidence:** `src/order_desk/agent/loop.py:111-112`: after `MAX_TOOL_ROUNDS` the reply is "Sorry, this needs a person. The shop will reply soon." and only a `gave_up` trace event is written; nothing is queued to the shop.
- **Impact:** the dealer waits for a reply nobody knows about (reference runtime only; Hermes has its own persona text).
- **Found by:** reading · **Fix direction:** call `outbox.notify_staff()` with the chat ID when giving up · Status: open

### KI-037 A long vision call may block the whole MCP server (unverified)
- **Evidence:** `src/order_desk/vision/reader.py:63,68,101-119`: up to 90 s per call, 3 client attempts (`max_retries=2`) and 2 JSON asks inside a synchronous tool. If `mcp>=2.3` runs synchronous tools on the event loop, `/health` and the other scope stall (`src/order_desk/db/connection.py` assumes worker threads). Hermes' MCP client timeout is unknown.
- **Impact:** one slow chitti read could freeze both bots. The SDK was not installed when this was written; it is now (KI-058), but this behaviour was not run.
- **Found by:** reading · **Fix direction:** check the SDK; run the call in a thread or lower the retry budget · Status: open

### KI-038 Outbox delivery edge cases
- **Evidence:** `src/order_desk/telegram/bot_api.py:42-43,100-111`: a multi-chunk text is retried whole, so earlier chunks repeat, and one line over 4000 characters is never split (400, permanent). `src/order_desk/services/outbox.py:90-114`: 20 rows at up to 30 s each can outlive the 60 s lease (duplicates if two senders run). `:63` `INSERT OR IGNORE` also hides CHECK and NOT NULL violations, for example a misspelt `bot`.
- **Impact:** occasional duplicate or lost messages; a typo in `bot` would be silent.
- **Found by:** reading · **Fix direction:** one key per chunk; hard-split long lines; use `ON CONFLICT(dedupe_key) DO NOTHING` · Status: open

### KI-039 The dev-bot replays the last update batch after a restart
- **Evidence:** `src/order_desk/agent/dev_bot.py:78-88` keeps the offset in memory and confirms it on the next `getUpdates`; `:73` histories are in memory; `:80-108` handles one update at a time.
- **Impact:** duplicate replies, and repeated appends (KI-006). Reference runtime only.
- **Found by:** reading · **Fix direction:** persist the offset after processing · Status: open

### KI-040 `/health` only proves the process is up
- **Evidence:** `src/order_desk/mcp_server/server.py:114-115`.
- **Impact:** it answers ok with a broken database or dead workers.
- **Found by:** reading · **Fix direction:** add a DB ping and the age of the oldest pending outbox row · Status: open

### KI-041 Charts use pyplot global state from worker threads
- **Evidence:** `src/order_desk/documents/charts.py:35,53` (inferred not thread-safe).
- **Impact:** garbled or failed charts under concurrency.
- **Found by:** reading · **Fix direction:** use the `Figure` API without pyplot · Status: open

### KI-042 Nothing is ever cleaned up
- **Evidence:** `src/order_desk/vision/images.py:73-78` adds an `uploads` row per resolve; nothing purges `data/uploads`, `data/generated`, `outbox`, `idempotency_keys` or `audit_log`.
- **Impact:** disk and database growth on a laptop.
- **Found by:** reading · **Fix direction:** a maintenance job with age limits · Status: open

### KI-066 The new-dealer card is lost when the owner has not pressed START in the shop bot, and nothing tells the owner later
- **Evidence:** live Telegram run, 2026-10-27 (devbot). `DealerDesk.register()` queued `messages.new_dealer_request()` with `outbox.notify_staff()` before the owner had pressed START in the shop bot. Telegram answered 400 "chat not found"; `BotApi._call()` marks 400 as permanent, so the sender set the row to `failed` on the first try (KI-031, KI-032). No tool listed pending dealers, so the owner never learned of the request.
- **Impact:** a dealer waits for an approval that the owner does not know about. The same holds for every other card or slip sent to a chat that never started the shop bot.
- **Found by:** live run · Status: fixed (2026-10-27, `get_report` has a new name `pending_work` that reads the work from the tables; the setup step is documented)
- **Fix:** `src/order_desk/services/reports.py` `pending_work()` returns `pending_dealers` (`dealers` in status `pending`) and `orders_waiting_for_approval` (`v_orders` in `AWAITING_OWNER`). `ShopDesk.report()` has the handler and `tools.py` `get_report` names it (no new tool: still 12 dealer, 22 shop, 33 distinct). `shop.md` has a section that makes the agent call it on `/start` and on "what is pending" and greet the owner with the summary. `README.md` Full setup step 7 and `RUNTIME_AND_OPERATIONS.md` §2.3, §10.1 and §12 say that the owner presses START in the shop bot before dealers register. Tests: `tests/test_pending_work.py`. Not changed: the outbox still drops the card (KI-031 and KI-032 stay open). A running devbot must be restarted, and the Hermes sandboxes must get the regenerated `hermes/` files, before the agent uses the new report (not done in this change).

### KI-069 A devbot reply was lost after one network error
- **Evidence:** live Telegram run, 2026-10-27 18:50:22: `send failed: sendMessage: network error: [Errno 54] Connection reset by peer` from `dev_bot._safe_send()`, which tried once.
- **Impact:** the dealer got no answer and had to ask again.
- **Found by:** live run · Status: fixed (2026-10-27, `_safe_send()` tries again after 1 s and 3 s)
- **Fix:** `src/order_desk/agent/dev_bot.py` `_safe_send()` tries up to 3 times (`SEND_RETRY_WAITS = (1.0, 3.0)`) while `TelegramError.retryable` is true (network error, 429, 5xx); 400, 401 and 403 are not retried. `waits` and `sleep` are arguments, so tests run at once. Tests: `tests/test_dev_bot.py` (they run the real `BotApi` error marks over `httpx.MockTransport`). Limits: a network error can come after Telegram took the message, so a reply can show twice; a reply over 4000 characters is sent in parts and a retry starts at part 1; `retry_after` is still ignored (KI-032). The `getUpdates` loop and the outbox sender are unchanged.

## 5. Tech debt

### KI-043 There is no schema migration path
- **Evidence:** `src/order_desk/db/connection.py:27` `SCHEMA_VERSION = "1"` is written at `:42-46` but never read; `src/order_desk/db/schema.sql` uses `CREATE TABLE IF NOT EXISTS` and views are dropped and recreated at every start; `:39` `with self._open() as conn:` commits but never closes the connection.
- **Impact:** a new column or constraint never reaches an existing `data/order_desk.sqlite3`. An index or view over a new column makes `initialize()` raise, so `serve`, `sender`, `scheduler`, `devbot` and `init` fail to start, and `executescript` runs statement by statement, so a failing view stays dropped. This blocks the fix for KI-002.
- **Found by:** reading · **Fix direction:** read `meta.schema_version` and apply numbered migrations; test an upgrade from the previous schema · Status: open

### KI-044 State names are hard-coded in about twelve places
- **Evidence:** `src/order_desk/domain/states.py:71-74` `OPEN_FOR_DEALER`, `HOLDS_RESERVATION` and `FINAL` are unused, while string lists repeat in `src/order_desk/services/invariants.py:14-15,48`, `shop_desk.py:83`, `dealer_desk.py:47,98,245`, `orders.py:261`, `reports.py:64-65,81`, `maintenance.py:30,47,65`, `evals/run_text2sql.py:33-34` and `evals/golden/text2sql.jsonl` (`mcb-sold`).
- **Impact:** a new or renamed state is silently missed.
- **Found by:** reading · **Fix direction:** use the groups in `states.py`; grep the literals before touching a state · Status: open

### KI-045 Dead or write-only code and columns
- **Evidence:** `src/order_desk/domain/errors.py:32-34` `NeedsDealerAnswer` is never raised (`dealer_desk.py:169-174` returns a dict); `src/order_desk/services/credit.py:27` `is_clear`; `src/order_desk/db/schema.sql:42` `dealers.credit_limit_paise`, `:139` `orders.owner_note`, `:250` `ix_uploads_sha`; `orders.linked_order_id` is stored and never read (it is not on the approval card).
- **Impact:** readers assume features that do not exist (OQ-16).
- **Found by:** reading · **Fix direction:** delete, or wire them in · Status: open

### KI-046 The reference agent depends on MCP SDK internals
- **Evidence:** `src/order_desk/agent/loop.py:27,77` imports `mcp.server.mcpserver.tools.base.Tool` and uses `Tool.from_function(...).fn_metadata.validate_arguments`; `:55` builds a whole `MCPServer` to read JSON schemas. `src/order_desk/mcp_server/server.py:22-23` imports `MCPServer` and `transport_security`. The pin is `mcp>=2.3,<3` (`pyproject.toml:13`). `src/order_desk/agent/skills_export.py:18` imports `agent.loop` only for `load_instructions`.
- **Impact:** an SDK minor release can break the dev-bot, `export-skills` and every conversation eval.
- **Found by:** reading · **Fix direction:** pin an exact tested version, or build the tool schemas without SDK internals · Status: open

### KI-047 The identity argument is found through `__code__.co_varnames`
- **Evidence:** `src/order_desk/mcp_server/tools.py:164,349`. `co_varnames` lists local variables as well, and only the scope's exact name is hidden.
- **Impact:** a shop tool that used `telegram_user_id` would not be hidden, and a local variable with that name would be misdetected. The failure lets the model choose who it is (KI-020).
- **Found by:** reading · **Fix direction:** use `inspect.signature`; test that every tool hides its identity parameter · Status: open

### KI-048 Direct imports that `pyproject.toml` does not declare
- **Evidence:** `pydantic` (`src/order_desk/mcp_server/tools.py:28`, `src/order_desk/agent/loop.py:28`) and `httpx2` (`tests/test_mcp_http.py:56`) are not in `pyproject.toml:12-21`.
- **Impact:** both are installed only because other packages need them: `pip show` in `.venv` on 2026-10-27 says `httpx2` 2.16.0 is required by `mcp` and `openai`, and `pydantic` 2.14.1 by `mcp`, `mcp-types` and `openai`. A future release of those could drop them.
- **Found by:** reading · **Fix direction:** declare `pydantic`; find out which package provides `httpx2` (OQ-06) · Status: open

### KI-049 Test gaps
- **Evidence:** by a static search of call sites, 16 of 33 desk actions have no direct pytest call. DealerDesk: `register`, `search_catalog`, `send_catalog`, `read_text_order`, `cancel`. ShopDesk: `list_orders`, `order_details`, `mark_dispatched`, `set_dealer_status`, `set_dealer_price`, `dealer_bills`, `add_product`, `add_alias`, `discard_stock_in`, `report`, `describe_views`. No test imports `src/order_desk/documents/catalog_pdf.py`, `src/order_desk/vision/reader.py` (`TokenFactoryReader`, `prepare_image`), `src/order_desk/vision/prompts.py`, `BotApi`, `src/order_desk/workers/scheduler.py`, `outbox_sender.main`, `src/order_desk/agent/dev_bot.py`, `src/order_desk/agent/skills_export.py`, `src/order_desk/cli.py`, `src/order_desk/demo.py` or `server.main`. No test checks that `hermes/*` matches `src/order_desk/agent/instructions/*`.
- **Impact:** KI-001 (fixed 2026-10-27, now with tests), KI-002 and KI-003 exist because of these gaps. `README.md` says every test also checks the invariants, but only tests that use the `deps` fixture do (DD-25).
- **Found by:** reading · **Fix direction:** add the missing tests (reopen then re-confirm is covered since 2026-10-27, KI-001) · Status: open

### KI-050 Tests are tied to one example label and load the real `.env`
- **Evidence:** `tests/test_evals.py:41` hard-codes `evals/golden/labels/easy_01.example.json` and `:61-63` asserts exact metrics for its 10 lines, so renaming it to `.checked.json` (the workflow in `evals/golden/README.md`) breaks 2 tests. `evals/common.py:39` `load_settings(REPO / ".env")` puts real values into `os.environ` inside the pytest process; `evals/common.py:25` and `tests/test_evals.py:17` change `sys.path`, which works only from the repo root.
- **Impact:** the labelling workflow breaks the suite; a developer's secrets reach the test process.
- **Found by:** reading · **Fix direction:** a dedicated fixture label; no `.env` in tests · Status: open

### KI-051 The evals cannot gate a change, and the scorecard is stale
- **Evidence:** every `evals/run_*.py` ends with `main()` and exits 0 whatever the metrics. `evals/results/SCORECARD.md` shows one 40-case matcher run and "Not run yet" for the rest. `evals/golden/labels/` holds 15 `*.example.json` and 0 `*.checked.json`, so `python -m evals.run_chit_reader` exits "No labels found" without `--include-examples`. `evals/run_text2sql.py:29-37` `PROMPT` includes the state list and a "Sold" definition that the production agent only gets as `ShopDesk.describe_views()` notes (`src/order_desk/services/shop_desk.py:413-415`).
- **Impact:** "must be 0" metrics cannot gate CI; the vision eval has never produced a number; text-to-SQL accuracy may be overstated.
- **Found by:** reading · **Fix direction:** exit non-zero below thresholds; check the 15 labels; use the production prompt in the eval · Status: open

### KI-052 Configuration parsing edge cases
- **Evidence:** `src/order_desk/config.py:22-28` has its own dotenv parser (no `export`, no inline comments) while `scripts/setup_nemoclaw.sh:18` runs `source .env` with bash rules. `config.py:93,101-103` `int("")` crashes `load_settings()` on an empty value. `:100` `MCP_ALLOWED_HOSTS` entries are not stripped (`"a, b"` keeps `" b"` and the host check rejects it).
- **Impact:** the same `.env` can mean different things to the two programs; small typos crash start-up.
- **Found by:** reading · **Fix direction:** strip and tolerate empty values; use one parser · Status: open

### KI-053 The seed data is fragile
- **Evidence:** `src/order_desk/db/seed.py:90-101` `_seed_old_bills()` hard-codes dealer IDs from `dealers.csv`; `:51-55` an `opening_stock` of 0 violates `CHECK (qty_delta <> 0)` and rolls back the whole seed.
- **Impact:** editing the demo CSVs breaks `order-desk init`.
- **Found by:** reading · **Fix direction:** look the IDs up; skip zero stock · Status: open

### KI-054 Layering smells and no static tooling
- **Evidence:** package cycle `services` and `documents` (`src/order_desk/services/dealer_desk.py` imports `documents/catalog_pdf.py`, which imports `services/pricing.py` `rupees`); `src/order_desk/services/shop_desk.py:29,493` imports the private `matching._convert_quantity`; `src/order_desk/mcp_server/server.py:81,139` `create_app(...) -> Starlette` returns `BearerAuth(app, tokens)`; `src/order_desk/services/invariants.py:14-15` redefines `HOLDS` and `AFTER_APPROVAL`. No ruff, black, mypy or pre-commit configuration and no Make target.
- **Impact:** style and type errors are caught only by reading.
- **Found by:** reading · **Fix direction:** move `rupees()` to a lower layer; add a linter configuration · Status: open

## 6. Environment

### KI-055 `python3` is 3.10.0 and no newer interpreter has the dependencies
- **Evidence:** `pyproject.toml` `requires-python = ">=3.11"`; `Makefile:2,11` `PY ?= python3`. Probe on 2026-10-27, before the venv was built: `python3` is 3.10.0 (pyenv shim); `~/.pyenv/versions/3.11.6/bin/python`, `~/.pyenv/versions/3.11.9/bin/python`, `/opt/homebrew/bin/python3.13` and `/opt/homebrew/bin/python3.14` lack pytest, mcp, reportlab, openai, httpx, PIL, matplotlib, pydantic, starlette and uvicorn; `uv` is not installed; the pyenv shim `python3.11` fails; there is no `.venv`.
- **Impact:** `make install` with the default `PY` fails, and so does the `python3 -m venv` quick start in `README.md`. `pytest` on 3.10 stops while importing `tests/conftest.py` (`No module named 'reportlab'`). There was no pass/fail baseline until 2026-10-27 (then 84 passed); `.pytest_cache/v/cache/nodeids` listed 84 ids from an earlier run with no `lastfailed` file.
- **Found by:** probe (commands run, nothing installed) · **Fix direction:** `make install PY=python3.13 && source .venv/bin/activate && pytest -q` (needs network); record the counts in `docs/codebase/CHANGE_GUIDE.md` §2.1 and `CLAUDE.md` · Status: fixed for this checkout (2026-10-27: `.venv` built with `/opt/homebrew/bin/python3.13` = 3.13.7 and `pip install -e ".[dev]"`; baseline 84 passed, recorded in those two files). Still true: `python3` on PATH is 3.10.0, so `make install` without `PY=` and the `README.md` quick start fail (DD-26)

### KI-056 The folder is not a git repository
- **Evidence:** there is no `.git` directory in the repo root, and the folder name ends in " 2" (a copy).
- **Impact:** no history and no undo, for any edit and for KI-004's delete.
- **Found by:** reading · **Fix direction:** `git init` and commit before changing code, or work on a copy · Status: fixed (2026-10-28, `git init` with a baseline commit; nothing pushed yet)

### KI-057 `make local` and `order-desk init` load demo data into the real database
- **Evidence:** `scripts/start_local.sh:9` runs `order-desk init`; `src/order_desk/db/seed.py` `seed_demo()` fills an empty catalog with demo dealers, products and old bills; there is no empty init. The banner at `scripts/start_local.sh:24-25` shows the shell `MCP_PORT`, not the `.env` value.
- **Impact:** a production start gets demo dealers and bills (DD-27).
- **Found by:** reading · **Fix direction:** add `init --empty`; make `make local` skip the demo seed · Status: open

### KI-058 SDK and deployed behaviour are unverified
- **Evidence:** the `mcp` SDK is not installed on this Mac, so every SDK claim in the docs is unverified (`MCPServer.streamable_http_app()` arguments, the Host check with `host="0.0.0.0"`, synchronous tools in a thread pool, `httpx2`). `FEEDBACK.md` Part B is empty and `README.md` Limits asks to confirm the photo path on the real bot: no recorded run of the Hermes/NemoClaw path.
- **Impact:** Hermes-path statements (KI-020, KI-037, DD-14) are inferred from code and comments.
- **Found by:** reading · **Fix direction:** install the dependencies and run the suite; run the real bot once and fill in `FEEDBACK.md` Part B · Status: open (partly checked 2026-10-27: the SDK is now installed in `.venv` (`mcp` 2.3.0) and `tests/test_mcp_http.py` passes, so the routes, `BearerAuth` 401s, scope separation, a tool call through the official client and `/uploads` that it covers have been run; the Host check with `host="0.0.0.0"`, thread-pool tools and the whole Hermes path are still unverified)

### KI-059 The default `AGENT_MODEL` is not in the Token Factory model list
- **Evidence:** on 2026-10-27 a free `GET https://api.tokenfactory.nebius.com/v1/models` (the default `NEBIUS_BASE_URL`, the key from `.env`) returned HTTP 200 and 30 models. It lists `nvidia/Nemotron-3.5-Super-VL` (the `VISION_MODEL` default) but not `nvidia/Nemotron-3.5-Lightning`, the `AGENT_MODEL` default in `src/order_desk/config.py:47,89` and `.env.example:9`. It does list `nvidia/Nemotron-3_5-Lightning` (underscore); `FEEDBACK.md` #10 already notes the two spellings.
- **Impact:** the calls that use the default chat model (`order-desk devbot`, `evals.run_text2sql`, `evals.run_conversations`, the `setup_nemoclaw.sh` onboarding) name an ID the provider does not list. A chat call with it was not tried. Both paid evals were left unrun for this reason; whether the underscore ID is the same model was not checked, and no other ID was substituted.
- **Found by:** probe (models-list call, no paid tokens) · **Fix direction:** the owner confirms the exact ID in the Token Factory console, then changes `config.py:47,89`, `.env.example:9`, `.env` and the two doc tables (`RUNTIME_AND_OPERATIONS.md` §5.2, `AGENT_MCP_AND_VISION.md`) together · Status: open

## 7. Doc drift in the human docs

Code wins. Each row says what the human doc claims and what the code does. Do not edit the human docs from here; tell the owner.

| ID | Human doc | Claim | What the code does | Evidence |
|---|---|---|---|---|
| DD-01 | `docs/06-reliability.md` §6.3 | Retry "10 s, 20 s, 40 s ... max 10 min" | `MAX_ATTEMPTS = 6`: waits 10, 20, 40, 80, 160 s, then `failed` (about 5 min); the 600 s cap is never reached | `src/order_desk/services/outbox.py:29,128-129` |
| DD-02 | `docs/06-reliability.md` §6.3 | Only "chat not found" fails permanently | Every status below 500 except 429 fails at once, including 400, 401, 403 | `src/order_desk/telegram/bot_api.py:86-89` |
| DD-03 | `docs/06-reliability.md` §6.2 | `confirm_order` past that state returns "already done" | Only AWAITING_OWNER short-circuits; PACKING and later raise `illegal_transition` | `src/order_desk/services/dealer_desk.py:163` |
| DD-04 | `docs/06-reliability.md` §6.2 | "Each tool is safe to repeat" | Not for append, `add_sku`, `read_supplier_invoice`, `send_chart`; second calls of `add_product`, `reopen_order`, `discard_stock_in` error. `docs/05-mcp-tools.md` admits only two | KI-006 |
| DD-05 | `docs/05-mcp-tools.md` §5.1 and `docs/06-reliability.md` §6.2 | `read_chitti_photo` is safe to call again and returns the existing read-back | `append=true` adds the lines again | `src/order_desk/services/dealer_desk.py:117,231-234` |
| DD-06 | `docs/06-reliability.md` table | `apply_stock_in`: unique supplier plus invoice number | The database UNIQUE counts discarded drafts, the app check skips them | `src/order_desk/db/schema.sql:219`; `src/order_desk/services/shop_desk.py:520-526` |
| DD-07 | `docs/05-mcp-tools.md:71` | Shop `search_catalog` needs "staff" | No identity argument and no `_staff()` check; same for dealer `search_catalog` and `send_catalog_pdf` | `src/order_desk/mcp_server/tools.py` `shop_tools()` |
| DD-08 | `docs/05-mcp-tools.md` | `send_catalog_pdf` is repeat-safe, "one per hour" | Only the outbox message is deduped per UTC hour; the PDF is rebuilt on every call | `src/order_desk/services/dealer_desk.py:79-83` |
| DD-09 | `docs/05-mcp-tools.md` error table | `needs_dealer_answer` is an error | `NeedsDealerAnswer` is never raised; `confirm()` returns a dict with that code and commits the `short` marks | `src/order_desk/domain/errors.py:32-34`; `dealer_desk.py:169-174` |
| DD-10 | `docs/09-security.md` §9.1 (lines 31-32) | Every message to a dealer goes to the stored chat ID; an impersonator never sees the victim's read-back | Read-backs, order status and profile are tool results returned to the caller; `send_catalog()` queues to the model-supplied ID | `src/order_desk/services/dealer_desk.py:39-50,75-84,215-227` (KI-020) |
| DD-11 | `docs/09-security.md` | Only `approve_order` removes stock | Owner-only `adjust_stock()` also writes negative `stock_movements` | `src/order_desk/services/shop_desk.py:231-243` |
| DD-12 | `docs/09-security.md` | Only JPEG, PNG, WEBP; 15 MB limit | Any RIFF file passes as WEBP; the size check runs after the whole body is read | `src/order_desk/vision/images.py:38`; `src/order_desk/mcp_server/server.py:118` |
| DD-13 | `docs/09-security.md` network-policy row | A sandbox network policy applies | Nothing in the repo configures one | `scripts/setup_nemoclaw.sh` (KI-030) |
| DD-14 | `docs/10-decisions.md` #12 (line 18) and `README.md:133` | Image references are `tg:` and `up:` | Four kinds; the main Hermes path is `sandbox:` (host CLI `nemohermes ... download`, not a bot token); `file:` is for tests and evals | `src/order_desk/vision/images.py` `ImageResolver.resolve()` |
| DD-15 | `docs/10-decisions.md` #4 (line 10) and `docs/07-evals.md:112` | New products need aliases; `alias_missing` is fixed with an alias | `match_line()` never reads `product_aliases`; they feed only catalog search | `src/order_desk/services/matching.py`; `dealer_desk.py:333` |
| DD-16 | `docs/04-order-states.md` | PARSED to CANCELLED exists; CONFIRMED and APPROVED are states an order is "in" | PARSED, CONFIRMED, APPROVED are entered and left in one transaction and never stored in `orders.state`; PARSED to CANCELLED is allowed but unreachable | `src/order_desk/services/dealer_desk.py` `_save_reading()`, `confirm()`; `shop_desk.py` `approve()` |
| DD-17 | `docs/04-order-states.md` §4.3 | A reply after the deadline gives EXPIRED | Only if the scheduler ran; `confirm()` never checks `dealer_check_deadline` | `src/order_desk/services/dealer_desk.py:159-183`; `maintenance.py:22-37` |
| DD-18 | `docs/04-order-states.md` | A blocked dealer: `start_order` refuses | Also `pending`; no other dealer tool checks status | `src/order_desk/services/orders.py:241` (KI-027) |
| DD-19 | `docs/04-order-states.md:69` and `src/order_desk/agent/instructions/dealer.md:72` | After confirming, `reopen_order` then continue and confirm again | Re-confirm failed; fixed 2026-10-27, code and docs agree now | KI-001 |
| DD-20 | `docs/03-database-schema.md` §3.3 | `audit_log` records stock counts | `adjust_stock()` writes only a `stock_movements` row; `add_alias()` and `discard_stock_in()` are not audited either | `src/order_desk/services/shop_desk.py:231-243` |
| DD-21 | `docs/03-database-schema.md:107` | Column lists of `v_dealer_credit` and `v_orders` | They omit `dealer_id`, `dealer_shop`, `source_kind`, `updated_at` | `src/order_desk/db/schema.sql` |
| DD-22 | `docs/07-evals.md:25,32,83` | 15 chitti images with human labels, `*.checked.json` | 0 checked labels (15 `*.example.json`); the default `run_chit_reader` exits "No labels found"; the scorecard says "Not run yet" | `evals/run_chit_reader.py:98-101`; `evals/results/SCORECARD.md` |
| DD-23 | `docs/07-evals.md:26,59`, `README.md:40,106` and `README.md:141` | Matcher "41 / 41" (README line 141 says 40 cases) | `matching.jsonl` has 41 lines; the only recorded run has 40 cases, so "41 / 41" is unverified | `evals/golden/matching.jsonl`; `evals/results/SCORECARD.md:12` |
| DD-24 | `README.md:134` | "34 tools in two scopes" | 12 + 22 registrations; 33 distinct names, because `search_catalog` is in both | `src/order_desk/mcp_server/tools.py` |
| DD-25 | `README.md` repository map | "Every test also checks the database invariants" | Only tests that use the `deps` fixture | `tests/conftest.py:85-87` |
| DD-26 | `README.md` Quick start and `docs/08-nemoclaw-setup.md` §8.2 | `python3 -m venv .venv` then `pip install -e .` | Fails where `python3` is 3.10.0 | `pyproject.toml` (KI-055) |
| DD-27 | `README.md` Full setup step 4 and `docs/08-nemoclaw-setup.md` §8.2 | `make local` starts the three processes | It first runs `order-desk init`, which loads demo data into an empty database | `scripts/start_local.sh:9` (KI-057) |
| DD-28 | `docs/08-nemoclaw-setup.md` §8.5 | Onboarding example | Omits `TELEGRAM_REQUIRE_MENTION=1`, which `onboard()` sets | `scripts/setup_nemoclaw.sh` |
| DD-29 | `docs/01-architecture.md` §1.4 | "Tools: MCP over HTTPS" | The server speaks plain HTTP; HTTPS comes from the external tunnel | `src/order_desk/mcp_server/server.py` docstring |
| DD-30 | `docs/01-architecture.md` §1.4 | Model-facing instructions live in two places | A third exists: MCP `initialize` text `DEALER_INSTRUCTIONS` and `SHOP_INSTRUCTIONS`, unused by the reference agent (it passes an empty string) | `src/order_desk/mcp_server/server.py`; `src/order_desk/agent/loop.py` `openai_tool_schemas()` |
| DD-31 | `docs/02-data-flow.md` §2.1 | A stuck RECEIVED order recovers when the dealer resends | The resend is cancelled as a duplicate | `src/order_desk/services/dealer_desk.py:117-125,241-248` (KI-005) |
| DD-32 | `docs/05-mcp-tools.md` (`get_report` row) and `docs/02-data-flow.md:105` | `get_report` takes today, pending_orders, debtors or low_stock | It also takes `pending_work` (2026-10-27); the dealer list in it is for the owner only | `src/order_desk/services/shop_desk.py` `report()` (KI-066) |

### Code comments and docstrings that no longer match

| ID | Where | Claim | What the code does |
|---|---|---|---|
| CD-01 | `src/order_desk/clock.py` docstring | "All code asks this module for the time" | `src/order_desk/db/schema.sql:356-359` `v_dealer_credit` uses SQLite `'now'` (KI-010) |
| CD-02 | `src/order_desk/services/orders.py:3-5` | "The facade in services/desk.py" | No such file; the facades are `dealer_desk.py` and `shop_desk.py` |
| CD-03 | `src/order_desk/domain/catalog_terms.py` docstring | The matcher and the seed catalog import from it | `services/matching.py` and `db/seed.py` do not import it (the seed trusts the CSV) |
| CD-04 | `src/order_desk/agent/dev_bot.py` comment | "never process an update twice" | True only within one process lifetime (KI-039) |
| CD-05 | `src/order_desk/db/schema.sql` comments | `uploads.source` is 'telegram' or 'upload'; `stock_movements.ref_type` is order, supplier_invoice or count; `order_events.actor` is `dealer:<id>` | Code also writes `sandbox` and `file`; the seed writes `seed`; the id is the Telegram user ID, not `dealers.id` |
| CD-06 | `src/order_desk/services/maintenance.py` `report_stuck_reads()` docstring | "The dealer resends" is the recovery | The resend is cancelled as a duplicate (KI-005) |

## 8. Open questions

Hermes, NemoClaw and the MCP SDK (nothing in the repo can answer these)
- OQ-01 How does Hermes pass the caller's Telegram ID to tool calls, and does the model see the Telegram message ID used as `source_ref` and `payment_ref`? Only the dev-bot's `describe_update()` writes `message_id=` (KI-020, KI-008).
- OQ-02 Does `mcp>=2.3` run synchronous tools in a worker thread? Does `streamable_http_app()` take `host` and `transport_security` as used, and does `host="0.0.0.0"` only turn off the automatic Host check? (KI-037)
- OQ-03 How does NemoClaw inject `mcp add --env DEALER_MCP_TOKEN`? `BearerAuth` accepts only `Authorization: Bearer <token>`. How does Hermes show a 401 or a transport error to the user?
- OQ-04 Does Hermes read Telegram with getUpdates or a webhook, and what does `TELEGRAM_REQUIRE_MENTION=1` do in private chats? (One reader per bot token: never run the dev-bot and a Hermes sandbox on the same token.)
- OQ-05 Is `scripts/setup_nemoclaw.sh` safe to run again (existing sandbox, existing `order-desk` MCP entry)?
- OQ-06 Which package provides `httpx2` (`tests/test_mcp_http.py:56`)? (KI-048) Answered 2026-10-27: `mcp` 2.3.0 and `openai` 3.39.0 both require it.
- OQ-07 Is `/sandbox/.hermes/cache/` (allowed in `SANDBOX_MEDIA_DIRS`) media only, or a wider cache?
- OQ-08 Does Hermes emit `[Image attached at: /sandbox/.hermes/image_cache/...]` as the code expects? Who produces `up:` references in production (`POST /uploads` exists, nothing calls it)? Does Hermes pass the MCP server `instructions` to the model, and is the text duplicated when it loads both `SOUL.md` and `SKILL.md`?
- OQ-09 What MCP client timeout, model temperature, token limit and tool-round limit does Hermes use? (KI-037)
- OQ-10 Which SQLite build runs in production (the dbstat and dbpage tables)? (KI-024)

Product and policy decisions
- OQ-11 Is re-confirm after `reopen_order` meant to work? (KI-001.) Answered: yes (`docs/04-order-states.md`, `dealer.md`, the `approval:{id}:{version}` key); fixed 2026-10-27 in `stock.reserve()`, which makes the `released` row active again.
- OQ-12 Should `adjust_stock()` refuse a count below the active reservations, or should approval re-check on-hand? (KI-003)
- OQ-13 Should blocked dealers be stopped from confirming orders they started before the block? (KI-027)
- OQ-14 Which day boundary should reports use, UTC or IST? (KI-010)
- OQ-15 Should a discarded invoice be re-addable, for example with a partial unique index? (KI-002)
- OQ-16 What are `dealers.credit_limit_paise`, `orders.linked_order_id`, `OPEN_FOR_DEALER`, `HOLDS_RESERVATION`, `FINAL` and `NeedsDealerAnswer` for? (KI-045)
- OQ-17 Is `MAX_ATTEMPTS = 6` (about 5 min) intentional, given the docs say "max 10 min"? What should happen to `failed` outbox rows: alert, resend tool? (KI-031)
- OQ-18 Should payment idempotency be per bill, and is a Telegram message ID a safe `payment_ref` across staff chats? (KI-008)
- OQ-19 Should packers see `debtors` and credit in order details? (KI-028)
- OQ-20 How should staff access be revoked? Should `init` seed demo data in production? How are the processes supervised (launchd), logs rotated and failed outbox rows re-queued? (KI-026, KI-057)
- OQ-21 Should the 24 h same-file window be a setting like `DUPLICATE_WINDOW_HOURS`?
- OQ-22 Telugu and other non-Latin text: the search tokenizer drops it and matching relies on the model normalising it. Is transliteration planned?
- OQ-23 Which way should `documents` and `services` depend on each other, and should `rupees()` move down? (KI-054)
- OQ-24 Are the default model IDs in `src/order_desk/config.py` (`vision_model`, `agent_model`) the exact Token Factory IDs (`FEEDBACK.md` #10)? Was the agent client's default `max_retries` left on purpose? Answered for the IDs on 2026-10-27 (free `GET /v1/models`): `vision_model` is listed, `agent_model` is not and the list has `nvidia/Nemotron-3_5-Lightning` (KI-059). `max_retries` is still open.
- OQ-25 Which environment produced the 84-id `.pytest_cache`, and does the suite pass today? (KI-055) Answered 2026-10-27: the suite passes (84 passed on Python 3.13.7, 86 after KI-001); which environment wrote the old cache is still unknown.

## 9. Found by the evals and the live runs (27 and 28 Oct 2026)

These items were found by running the evals and the bots, not by reading the code. Checked against the code on 2026-10-28.

### KI-060 The vision model's thinking used the whole output budget
- Found in the first chitti-reader eval on 2026-10-27. The thinking counts inside `max_tokens`. With 4000, one call spent 3906 tokens thinking; the JSON was cut off or never started, and 10 of 15 chittis failed.
- Status: fixed (2026-10-27, `VISION_MAX_TOKENS` with default 16000 in `config.py` and `vision/reader.py`, plus a cut-off warning; tests in `tests/test_vision_reader.py`). Platform feedback item (`FEEDBACK.md` section 3).

### KI-061 The SQL guard refused every LIKE search
- Found in the text-to-SQL eval on 2026-10-27. SQLite runs `x LIKE y` as a call of the function `like(y, x)`, and the authorizer refused that call, so every name search failed.
- Status: fixed (2026-10-27, `like` added to `_SAFE_FUNCTIONS` in `services/reports.py`; a case in `tests/test_readonly_sql.py`).

### KI-062 (number not used)

### KI-063 The conversation eval cut off the dealer's last message
- Found in the conversation eval on 2026-10-27. The simulated dealer sends "done" together with its final message ("Yes.", "Cancel it."). The harness stopped before the agent got that message, so the order was never confirmed or cancelled.
- Status: fixed (2026-10-27, `evals/run_conversations.py` gives the final message to the agent; a test in `tests/test_evals.py`).

### KI-064 The conversation eval gave the agent an unknown image reference
- Found in the conversation eval on 2026-10-27. The harness sent "file:<path>", a form that the instructions and the tool text do not list. The model rewrote it, every read failed, and no order left RECEIVED.
- Status: fixed (2026-10-27, the harness stores the photo and sends an `up:` reference; a test in `tests/test_evals.py`).

### KI-065 The agent asked for YES when there was no read-back
- Found in the conversation eval on 2026-10-27. In three runs the photo could not be read, but the agent still told the dealer "reply YES to confirm".
- Status: fixed (2026-10-27, a rule in `agent/instructions/dealer.md`: never ask for YES unless a tool result in this turn gave a `readback`; a test in `tests/test_agent_loop.py`).

### KI-070 Shop bot treated any photo as a supplier invoice
- Found in the live run on 2026-10-28: a dealer's chitti sent to the shop bot became a stock-in draft (nothing was applied; it needed the supplier, the invoice number and OK).
- Status: fixed (2026-10-28, `shop.md`: ask "supplier invoice?" before `read_supplier_invoice` unless the caption says so).

### KI-071 Internal state names in agent replies
- Found in the live run on 2026-10-28: "(state=AWAITING_OWNER)", "State: PACKED".
- Status: fixed (2026-10-28, a plain-words rule in `dealer.md` and `shop.md`).

### KI-072 "YES" with two open orders had no instruction
- Found by the coverage matrix on 2026-10-28; `docs/04` says the agent asks which order.
- Status: fixed (2026-10-28, a `dealer.md` rule: call `get_order_status` and ask which order). There is no eval case yet.

### KI-073 Chart x-axis showed "label"
- Found in the live run on 2026-10-28: `shop.md` said "(label, number)" and the model used `label` as the column name.
- Status: fixed (2026-10-28, `shop.md`: give the columns plain names with SQL aliases).

### KI-074 Token Factory HTTP 529 (overloaded)
- Found in the live run on 2026-10-28 at 09:15 IST, on a `nvidia/Nemotron-3.5-Super-VL` call. The OpenAI client's retry recovered about 34 s later.
- Status: mitigated (2026-10-28, `max_retries` 2 -> 4 in `agent/dev_bot.py` and `vision/reader.py`). Platform feedback item.
