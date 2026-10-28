# chitti-order-desk

## What this is
A Telegram order desk for electrical dealers. A dealer sends a handwritten order list (a "chitti") as a photo or text. A vision model reads it, code matches the items to the catalog and holds stock, and the shop owner approves; approval makes a packing slip and a bill.
Python 3.11+, one SQLite file, an MCP server with two token-scoped tool sets, two Telegram bots. The model reads and talks; the code decides.
Deep docs are in `docs/codebase/` (map below). Read only the one you need. Paths like `services/orders.py` are relative to `src/order_desk/`.

## Environment and traps
- The repo path has a space (`chitti-order-desk 2`): quote it in every shell command.
- Git: a local repo since 2026-10-28 (branch `main`; `origin` = the public GitHub repo). Nothing is pushed yet: push only on the user's explicit OK. `_handoff/` (local working notes) and `.claude/` are gitignored.
- Python >= 3.11 is required (`pyproject.toml`); `python3` on PATH is 3.10.0 (pyenv shim) and too old. `.venv/` was built on 2026-10-27 with `/opt/homebrew/bin/python3.13` (3.13.7; a 3.11.9 is at `~/.pyenv/versions/3.11.9/bin/python`). Call `.venv/bin/python -m ...` or activate it. Baseline: 84 passed; 86 after the KI-001 fix; 159 after the live-run fixes, 160 after KI-075 (125 before them; KI-055).
- Dependencies (`pyproject.toml`): `mcp>=2.3,<3`, `openai>=1.40`, `httpx`, `uvicorn`, `starlette`, `reportlab`, `matplotlib`, `pillow`; dev: `pytest>=8`. Imported but undeclared: `pydantic`, `httpx2` (KI-048; installed through `mcp` and `openai`). Installed and tested: `mcp` 2.3.0, `openai` 3.39.0, `pytest` 9.1.1, `reportlab` 5.1.0, `matplotlib` 3.11.2, `pillow` 13.0.0. Installing needs the network: ask first.
- Secrets live in `.env` (gitignored; the repo has only `.env.example`). Never print or write token values (`NEBIUS_API_KEY`, `*_BOT_TOKEN`, `*_MCP_TOKEN`, `UPLOAD_TOKEN`). `.env` exists: the Nebius key, both bot tokens, both MCP tokens, the owner ID, `AGENT_MODEL=nvidia/nemotron-3-super-120b-a12b` and a Sarvam key (video voice-over only).
- Runtime data is under `data/` (gitignored: SQLite DB, uploads, logs). Do not edit or delete it by hand.
- Run every process from the repo root: `.env`, `data/` and file paths stored in outbox rows are relative to the current directory.
- `order-desk demo --workdir X` deletes X (KI-004). `order-desk init` and `make local` load demo data into an empty DB (KI-057).
- Never run paid evals or anything that calls Telegram or the model unless the user asks.

## Commands (repo root; [v] verified here on 2026-10-27 or 28, [u] not run)
- Venv [v]: `/opt/homebrew/bin/python3.13 -m venv .venv && .venv/bin/python -m pip install -q -e ".[dev]"` (23 s; the steps of `make install PY=python3.13`). It also writes the gitignored `src/chitti_order_desk.egg-info/`. `source .venv/bin/activate` puts `pytest` and `order-desk` on PATH; the other Make targets need that.
- `.venv/bin/python -m pytest -q --tb=line` (= `make test` with the venv active) [v]: **160 passed**, about 4 s, all offline (125 before the live-run fixes, 84 before the KI-001 tests). One test: `.venv/bin/python -m pytest -q tests/test_order_flow.py::test_approve_twice_deducts_once`.
- `PYTHONPATH=src python3 -B -m order_desk.cli --help` [v, works on 3.10]. `--env PATH` goes before the subcommand.
- `order-desk init` [u] creates the DB and demo data. `order-desk demo` [v] runs one full order offline in `data/demo` (no keys, no network).
- `make local` [u] = init + serve + sender + scheduler, logs in `data/logs/`; `curl http://127.0.0.1:8710/health` [u].
- `order-desk serve`, `sender` and `scheduler` [v, started by `scripts/start_local.sh`]: `serve` needs `DEALER_MCP_TOKEN` and `SHOP_MCP_TOKEN`; `sender` needs both bot tokens.
- `order-desk devbot --role dealer|shop` [v, live runs on 2026-10-27 and 28] is the reference runtime without Hermes; never on a bot token a Hermes sandbox uses.
- `order-desk export-skills` (= `make skills`) [v] after editing `agent/instructions/*.md`.
- `.venv/bin/python -m evals.run_matching` (= `make eval-offline`) [v]: 41 of 41 cases, accuracy 1.0, no network; writes `evals/results/matching/<stamp>/`. `evals.run_chit_reader`, `run_text2sql` and `run_conversations` are paid; they ran on 2026-10-27 (README "Evals", `evals/results/SCORECARD.md`). The reader scores against the 15 `*.checked.json` labels.
- Free models-list check [v]: `GET {NEBIUS_BASE_URL}models` with `Authorization: Bearer $NEBIUS_API_KEY` returns 200 and 30 models. Do it before any paid eval.
- No lint, format or type-check is configured.

## Architecture in brief
- Processes: `serve` (MCP over HTTP: `/dealer/mcp` 12 tools, `/shop/mcp` 22 tools, 33 distinct because `search_catalog` is in both; plus `/uploads` and `/health`), `sender` (outbox to Telegram), `scheduler` (expiry and reminders every 60 s). The design runs the model in Hermes inside a NemoClaw sandbox (not run: the macOS install failed); today `order-desk devbot` runs it in process through `agent/loop.py`.
- Layering rule (imports point down): `cli` and `demo` -> `agent` -> `mcp_server` and `workers` -> `services` -> `vision` and `documents` -> `db` -> `clock` and `config`. `domain/` is pure (imports only itself). `telegram/` is a leaf client. Sub-package `__init__.py` files are empty: import concrete modules.
- In `services/`, helpers (`orders`, `stock`, `credit`, `pricing`, `matching`, `outbox.enqueue`) take an open connection and never open or commit a transaction or call the network. `DealerDesk` and `ShopDesk` are the only facades: one method per user action, each owns its transaction, they never import each other. `mcp_server/tools.py` is a thin wrapper over them.
- Existing cross-edges, do not add more: `documents/catalog_pdf.py` -> `services/pricing.py`; `services/shop_desk.py` -> `matching._convert_quantity`.

## Domain essentials
- Glossary:
  - chitti or chit: the handwritten order list (identifiers say `chit`). Read-back: code-built text of how each line was read; `(?)` marks a doubtful line.
  - dealer: a customer, identified by Telegram user ID. staff: owner or packer. SKU: product text key like `WIRE-1.5-RED-FINOLEX`.
  - default brand: the pick when a line names none (the line becomes `assumed`). reservation: stock held for a confirmed order. sellable = on hand - reserved - safety stock (`v_stock`).
  - outbox: messages queued in the state-change transaction and sent later by `sender`. `version`: optimistic counter on `orders`.
- Order states: RECEIVED -> AWAITING_DEALER -> AWAITING_OWNER -> PACKING -> PACKED -> DISPATCHED; ends REJECTED, EXPIRED, CANCELLED. PARSED, CONFIRMED, APPROVED live inside one transaction only (in `order_events`, never in `orders.state`). Legal moves: `domain/states.py` `TRANSITIONS`.
- Invariants (`services/invariants.py` `check()`; asserted after every test that uses the `deps` fixture, never run in production):
  - stock is never negative or over-reserved; order stock leaves only after approval; an approved order has stock movements and a bill;
  - reservations exist only while CONFIRMED or AWAITING_OWNER; only legal moves are recorded; no open question reaches the owner.
- `reservations` keeps one row per (order, SKU) for the order's whole life (`UNIQUE (order_id, sku)`, all statuses): `release()` marks it `released`, and `reserve()` makes that row `active` again. Never INSERT a second row (KI-001).
- Money is INTEGER paise (`services/pricing.py` `rupees()` prints it). Time is UTC text `YYYY-MM-DDTHH:MM:SSZ` from `clock.py` only. Stock is derived from append-only `stock_movements` plus `reservations`.

## Rules for changing code
1. No network (model, Telegram, `nemohermes`) inside `Database.write()`. Pattern: read txn, network, short write txn that re-checks `orders.version` (`DealerDesk.read_chit()` and `_save_reading()`).
2. Change `orders.state` only through `services/orders.py` `transition()`; add the move to `TRANSITIONS` first. State names are hard-coded in about 12 places: grep before adding or renaming one.
3. Messages to people only through `outbox.enqueue()` or `notify_staff()` on the same connection as the state change, with a stable `dedupe_key`. Only `sender` sends. Wording is in `services/messages.py`; tests assert substrings, so grep `tests/` before rewording.
4. Time only through `clock.now_iso()` and `iso_in()`, never `datetime.now()`; tests freeze it with the `fixed_clock` fixture. Stock changes only through `services/stock.py` `add_movement()`; order stock leaves only in `ShopDesk.approve()` (the owner's shelf count `adjust_stock()` is the other writer, KI-003).
5. Services raise `OrderDeskError` subclasses (`domain/errors.py`). `tools._safe()` turns them into `{"ok": false, "error", "message"}`; any other exception, including `sqlite3.IntegrityError`, becomes `internal_error`.
6. New MCP tool: a method on `DealerDesk` or `ShopDesk`, plus a closure in `dealer_tools()` or `shop_tools()` appended to `fns` in `mcp_server/tools.py`. The caller-ID parameter must be named exactly `telegram_user_id` (dealer) or `staff_telegram_user_id` (shop), or the model can choose who it is. Shop methods call `_staff()` with the allowed roles. Docstrings and `Field(description=)` are the model-facing text. Nested values inside `changes` or `edits` are not coerced by the SDK (`line_index` must be a JSON int).
7. Persona text lives only in `agent/instructions/{dealer,shop}.md`; then run `order-desk export-skills`. Never hand-edit `hermes/` (generated; `tests/test_agent_loop.py` checks the sync).
8. `db/schema.sql` runs at every start but existing databases are not migrated (`SCHEMA_VERSION` is unused). A new column needs an upgrade path or a documented reset. Text-to-SQL may read only `REPORT_VIEWS` in `services/reports.py`; a new view also needs `_VIEW_BASE_TABLES`, a case in `tests/test_readonly_sql.py` and one in `evals/golden/text2sql.jsonl`.
9. Vocabulary (categories, units, brands, spec keys) lives only in `domain/catalog_terms.py`. The matcher uses category, spec, brand, default brand, unit and pack size; `product_aliases` feed catalog search, not matching. A matcher or catalog change must keep `python -m evals.run_matching` at 100% (41 cases). The vision JSON shape equals the golden label shape: change `vision/prompts.py`, `domain/chitti.py` and `evals/golden/README.md` together.
10. A new setting goes in `config.py` (`Settings` and `load_settings()`) and `.env.example`. Tests never touch the network (`FakeReader` in `tests/conftest.py`).

## Top traps (details and fixes in `docs/codebase/KNOWN_ISSUES.md`)
- KI-001 (fixed 2026-10-27): re-confirm after `reopen_order` works; `stock.reserve()` makes the order's `released` row `active` again. `test_confirm_again_after_reopen_*` cover it.
- KI-059: Token Factory model IDs mix styles (`nvidia/Nemotron-3_5-Lightning`, `nvidia/nemotron-3-super-120b-a12b`). Copy IDs from the free `GET /v1/models`. The `config.py` default is the listed Lightning ID; `.env` runs the agent on Super 120B.
- KI-020: the caller ID is a tool argument. Under Hermes a wrong ID returns another dealer's order data, though `docs/09-security.md` says it cannot.
- KI-031: the outbox gives up after about 5 minutes (6 attempts), and 400, 401, 403 fail at once. Nothing resends or alerts.
- KI-002: a discarded supplier invoice cannot be entered again (`UNIQUE (supplier, invoice_no)`).
- KI-003: a shelf count below the reserved quantity breaks the stock invariant.
- KI-043: no schema migrations; edits to `db/schema.sql` do not reach existing databases.
- KI-035: orders expire and owners get reminders only while `order-desk scheduler` runs; `confirm()` ignores the deadline.
- KI-021: the bot token is in request URLs and logs run at INFO, so it probably lands in `data/logs/`.
- KI-004: `demo --workdir` deletes a directory. Git restores tracked files only, not `data/`.
- The human docs lag the code (32 known mismatches, KNOWN_ISSUES section 7): for example "max 10 min" retries, "34 tools", `tg:` and `up:` image references.

## Docs map: changing X, read Y
| Changing or asking | Read |
|---|---|
| Where is X? File index, import graph, tests per tool | `docs/codebase/CODEBASE_MAP.md` |
| Processes, CLI, Make, `.env`, clock, workers, Telegram client, MCP auth, Hermes/NemoClaw deploy | `docs/codebase/RUNTIME_AND_OPERATIONS.md` |
| Tables, states, invariants, matching, stock, credit, pricing, error model | `docs/codebase/DOMAIN_AND_DATA_MODEL.md` |
| An MCP tool, instructions, text-to-SQL, vision prompts and models, PDFs and charts | `docs/codebase/AGENT_MCP_AND_VISION.md` |
| One request end to end: transactions, idempotency, outbox, error paths | `docs/codebase/DATA_FLOW.md` |
| Conventions, how to test, evals, change recipes, gotchas | `docs/codebase/CHANGE_GUIDE.md` |
| Bugs, risks, doc drift, open questions | `docs/codebase/KNOWN_ISSUES.md` |

Human docs hold design intent and may lag the code; the code wins (see KNOWN_ISSUES section 7). Do not edit them unless asked.
| Human docs | About |
|---|---|
| `docs/01-architecture.md`, `docs/02-data-flow.md` | big picture and journeys |
| `docs/03-database-schema.md`, `docs/04-order-states.md` | tables and state rules |
| `docs/05-mcp-tools.md`, `docs/06-reliability.md` | tool catalog; retries and idempotency |
| `docs/07-evals.md`, `docs/08-nemoclaw-setup.md` | eval design; deployment steps |
| `docs/09-security.md`, `docs/10-decisions.md` | threat model; why decisions were made |
| `README.md`, `FEEDBACK.md`, `docs/live-runs.md` | quick start and status; hackathon feedback with evidence; the live Telegram runs |

## Definition of done
- The change follows the rules above. `pytest -q` passes on Python >= 3.11 (160 tests today) and new behaviour has a test that names one rule.
- Matcher, catalog or vocabulary change: `run_matching` is 100%. Prompt, instruction or read-back change: run the relevant paid eval if the user allows it, otherwise say it was not run.
- Instructions changed: `order-desk export-skills`. Tool added: both scope lists, a test, and the counts (12 dealer, 22 shop, 33 distinct).
- Schema changed: an upgrade path or a documented reset. Env var added: `config.py` and `.env.example`. `order-desk demo` still runs offline. No secrets in code, docs or logs.
- Say what you could not run (the tests need Python >= 3.11 with the `.[dev]` extras, KI-055).

## Keep docs current
Update the doc in the same change, and bump its `Verified against code on` date only for sections you re-checked.
- CLI, Make, scripts, config, workers, outbox, Telegram, server, deploy: `RUNTIME_AND_OPERATIONS.md`. Schema, states, invariants, service rules, seeds: `DOMAIN_AND_DATA_MODEL.md`. Tools, instructions, SQL guard, vision, models: `AGENT_MCP_AND_VISION.md`. A journey, transaction or dedupe key: `DATA_FLOW.md`. Tests, evals, conventions, recipes: `CHANGE_GUIDE.md`. Files or symbols added, moved or deleted: `CODEBASE_MAP.md`.
- Found a bug or a drift: add a `KI-NNN` item (next free number). Fixed one: set `Status: fixed (date, change)` and correct the doc that described it.
- Keep the `pytest` counts current in `CHANGE_GUIDE.md` section 2.1, in the Environment section and in Commands above (160 passed on 2026-10-28).
- Keep this file at 100-150 lines (hard cap 170). Use plain relative paths, never import directives: imported files load into every session.
