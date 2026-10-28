# 5. MCP tools

The tools are in [`mcp_server/tools.py`](../src/order_desk/mcp_server/tools.py).
Each scope has its own URL and its own bearer token. A sandbox holds one token,
so the dealer agent cannot see or call a shop tool.

| Scope | URL | Token | Tools |
|---|---|---|---|
| dealer | `/dealer/mcp` | `DEALER_MCP_TOKEN` | 12 |
| shop | `/shop/mcp` | `SHOP_MCP_TOKEN` | 22 |
| upload | `/uploads` | either token, or `UPLOAD_TOKEN` | raw image bytes -> `{"image_ref": "up:..."}` |

All tools return a JSON object. On success it has `"ok": true`. On a known
problem it has `"ok": false`, an `error` code and a `message` for people:

| `error` | Meaning | What the agent does |
|---|---|---|
| `not_found` | No such order / product / line (also used for another dealer's order) | Tell the person |
| `not_allowed` | Blocked dealer, not staff, wrong role | Tell the person |
| `illegal_transition` | The order is not in a state that allows this | Get the status, explain |
| `invalid_input` | Bad argument (for example qty 2.5) | Ask again |
| `needs_dealer_answer` | Some lines need an answer before confirming | Send the read-back |
| `not_enough_stock` | Stock changed meanwhile | Send the read-back |
| `conflict` | Somebody changed the same record at the same time | Call again |
| `external_service_error` | Model API or Telegram failed | Wait, call again once |
| `internal_error` | A bug (logged on the server) | Apologize, stop |

## 5.1 Dealer tools

| Tool | Purpose | Safe to call again? |
|---|---|---|
| `get_dealer_profile` | Status (unknown/pending/active/blocked) and open orders | yes (read) |
| `register_dealer` | New dealer asks to join; owner gets a card | yes (one row per Telegram ID) |
| `search_catalog` | Find products and SKUs | yes |
| `send_catalog_pdf` | Queue the catalog PDF with the dealer's own prices | yes (one per hour) |
| `start_order` | Create an order for a message | yes (key = dealer + message ID) |
| `read_chitti_photo` | Download, duplicate check, read, match, stock check -> read-back | yes (returns the existing read-back) |
| `read_text_order` | The same for a typed order | yes |
| `update_order_lines` | Apply the dealer's answers | each call is one edit |
| `confirm_order` | Reserve stock, send to the owner | yes (returns "confirmed") |
| `reopen_order` | Back to the dealer before the owner decides | no (second call: illegal transition) |
| `cancel_order` | Cancel before approval | yes |
| `get_order_status` | One order, or the recent orders | yes |

`update_order_lines` change forms:

```json
[{"line_index": 3, "qty": 10},
 {"line_index": 4, "sku": "SW-6A-GM"},
 {"line_index": 5, "accept": true},
 {"line_index": 6, "remove": true},
 {"add_sku": "TAPE-3M", "qty": 5}]
```

## 5.2 Shop tools

| Tool | Who | Purpose |
|---|---|---|
| `list_orders`, `get_order_details` | staff | Orders, with credit, stock left and history |
| `approve_order` | owner | Edits (qty / remove), prices, stock removal, bill, packing slip, dealer notice |
| `reject_order` | owner | Release stock, tell the dealer the reason |
| `mark_packed`, `mark_dispatched` | staff | Tell the dealer |
| `set_dealer_status` | owner | Approve, block or unblock a dealer |
| `set_dealer_price` | owner | A special price for one dealer |
| `get_dealer_bills`, `record_payment` | owner | Credit; a payment is keyed by `payment_ref`, so it is never recorded two times |
| `adjust_stock` | owner | Set stock to a shelf count; the difference is recorded |
| `add_product`, `add_product_alias` | owner | Catalog changes |
| `read_supplier_invoice`, `update_stock_in`, `apply_stock_in`, `discard_stock_in` | owner | Stock-in from an invoice photo |
| `get_report` | staff | today, pending_orders, debtors, low_stock |
| `describe_reporting_views`, `run_readonly_query`, `send_chart` | owner | Text-to-SQL and charts |
| `search_catalog` | staff | Products and SKUs |

## 5.3 Images: `image_ref`

| Form | Source |
|---|---|
| `sandbox:<path>` | A photo in the Hermes media cache inside the sandbox (`[Image attached at: <path>]`). The server copies it with `nemohermes <sandbox> download`. Only media-cache folders are allowed. |
| `tg:<file_id>` | A Telegram file. The server downloads it with the bot token of the scope (reference runtime). |
| `up:<upload_id>` | An image sent first to `POST /uploads`. |
| `file:<path>` | Tests and evals only. The server refuses it. |

Why not send the image in the tool call? NemoClaw limits an MCP request to
131,072 bytes, and a phone photo is 1-5 MB. Also, a model should never copy
megabytes of base64 into a tool argument.
