# 2. Data flow

This guide follows the data through the five main jobs. For each step it
shows where the data is, and what crosses the network.

## 2.1 A dealer sends a chitti

```mermaid
sequenceDiagram
    autonumber
    participant D as Dealer (Telegram)
    participant H as Hermes (dealer-desk)
    participant M as MCP server
    participant DB as SQLite
    participant TF as Token Factory
    participant W as Outbox sender
    participant O as Owner (Telegram)

    D->>H: photo of chitti
    H->>M: get_dealer_profile(telegram_user_id)
    M->>DB: dealer status
    H->>M: start_order(photo, message_id)
    M->>DB: INSERT order (RECEIVED), key = dealer + message
    H->>M: read_chitti_photo(order_id, "sandbox:<cached path>")
    M->>M: copy photo out (nemohermes download), SHA-256
    M->>DB: same file in the last 24 h? -> CANCELLED as duplicate
    M->>TF: image + prompt (Nemotron 3.5 Super VL)
    TF-->>M: JSON lines (raw text, item, qty, doubt)
    M->>DB: match lines, check stock, PARSED -> AWAITING_DEALER
    M-->>H: readback text
    H-->>D: readback (numbered lines, "?" marks)
    D->>H: "line 10 is the 3-module Anchor box"
    H->>M: update_order_lines(...)
    H-->>D: new readback
    D->>H: "yes"
    H->>M: confirm_order
    M->>DB: reserve stock, CONFIRMED -> AWAITING_OWNER, queue approval card
    W->>DB: claim message
    W->>O: NEW ORDER #12 ... Credit: ... Reply APPROVE 12
```

**What crosses the network**
- Telegram -> Hermes: the dealer's message and photo. Hermes caches the photo
  in the sandbox and tells the agent its path.
- Hermes -> MCP server: tool calls of a few hundred bytes. The photo itself
  never goes through the tool call: NemoClaw limits an MCP request to 128 KB,
  and a phone photo is larger. The server copies the cached photo out of the
  sandbox with `nemohermes <sandbox> download` (the reference runtime uses the
  Telegram file ID instead).
- MCP server -> Token Factory: the photo, made smaller (max 2000 px, JPEG).
- Outbox sender -> Telegram: the approval card.

**Steps 7 to 11 are safe to repeat.** If the laptop stops between them, the
order stays in RECEIVED and holds no stock. The next `read_chitti_photo`
call does the work again. See [06-reliability.md](06-reliability.md).

## 2.2 The owner approves

```mermaid
sequenceDiagram
    participant O as Owner
    participant H as Hermes (shop-desk)
    participant M as MCP server
    participant DB as SQLite
    participant W as Outbox sender
    participant P as Packer
    participant D as Dealer

    O->>H: APPROVE 12 with line 6 = 4
    H->>M: approve_order(12, edits=[{line 6, qty 4}])
    M->>DB: ONE transaction: edit lines, fix prices, remove stock,<br/>make bill, APPROVED -> PACKING,<br/>queue packing slip + dealer notice
    W->>P: PACKING SLIP #12
    W->>D: Order #12 is approved. Line 6: 5 -> 4. Bill. Cash discount dates.
    P->>H: PACKED 12
    H->>M: mark_packed(12)
    M->>DB: PACKING -> PACKED, queue dealer notice
    W->>D: Order #12 is packed.
```

## 2.3 The owner adds stock from a supplier invoice

```mermaid
sequenceDiagram
    participant O as Owner
    participant H as Hermes (shop-desk)
    participant M as MCP server
    participant TF as Token Factory
    participant DB as SQLite

    O->>H: photo of the invoice
    H->>M: read_supplier_invoice("sandbox:<cached path>")
    M->>TF: read the invoice
    M->>DB: STOCK-IN DRAFT (stock does not change)
    H-->>O: preview: "MCB 32A: 38 -> 62 (+24)"
    O->>H: OK
    H->>M: apply_stock_in(draft)
    M->>DB: one stock_movements row per line, draft -> applied
```

The same invoice (supplier + number) cannot be applied two times. A pack
("2 box") becomes sell units with the product's `pack_size` (2 x 12 = 24).

## 2.4 The owner asks a question

1. Common questions use a fixed report: `get_report("today" | "pending_orders" | "debtors" | "low_stock")`.
2. Other questions: the agent reads `describe_reporting_views`, writes one
   SELECT, and calls `run_readonly_query`. The guard lets it read only the
   `v_*` views, on a read-only connection, with a row and time limit.
3. "Show it as a chart": `send_chart` draws a PNG and queues it in the outbox.

## 2.5 Timed jobs

| Every minute | What happens |
|---|---|
| Expiry | AWAITING_DEALER past its deadline -> EXPIRED; the dealer is told. |
| Reminder | AWAITING_OWNER longer than 2 h -> one reminder per 2 h window. |
| Stuck reads | Orders in RECEIVED for 15 min are logged (they hold no stock). |
