-- =============================================================================
-- Chitti Order Desk: database schema (SQLite)
--
-- This database IS the shop's CRM. The agent's memory is never the source of
-- truth; every fact the agent says comes from these tables.
--
-- Conventions
--   * Money is stored as INTEGER paise (1 rupee = 100 paise). No floats.
--   * Time is stored as TEXT in UTC ISO-8601: 'YYYY-MM-DDTHH:MM:SSZ'.
--     This sorts correctly and stays readable for text-to-SQL questions.
--   * Stock is never stored as one number that we overwrite. Each change is a
--     row in stock_movements, and the stock on hand is the SUM of those rows.
--   * Tables that start with v_ are VIEWS. Text-to-SQL may read ONLY views.
-- See docs/03-database-schema.md for the reasons behind each table.
-- =============================================================================

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- -----------------------------------------------------------------------------
-- People
-- -----------------------------------------------------------------------------

-- Dealers order through the dealer bot. Telegram private chat ID = user ID.
CREATE TABLE IF NOT EXISTS dealers (
    id                  INTEGER PRIMARY KEY,
    telegram_user_id    INTEGER NOT NULL UNIQUE,
    name                TEXT    NOT NULL,
    shop_name           TEXT,
    phone               TEXT,
    city                TEXT,
    -- pending: asked to join, owner did not approve yet
    -- active : may order
    -- blocked: owner stopped this dealer; the bot does not take orders
    status              TEXT    NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'active', 'blocked')),
    payment_terms_days  INTEGER NOT NULL DEFAULT 60,
    credit_limit_paise  INTEGER,            -- NULL = no limit set
    created_at          TEXT    NOT NULL
);

-- Staff use the shop bot. Role decides which shop tools a person may use.
CREATE TABLE IF NOT EXISTS staff (
    id                INTEGER PRIMARY KEY,
    telegram_user_id  INTEGER NOT NULL UNIQUE,
    name              TEXT    NOT NULL,
    role              TEXT    NOT NULL CHECK (role IN ('owner', 'packer')),
    active            INTEGER NOT NULL DEFAULT 1
);

-- -----------------------------------------------------------------------------
-- Catalog and prices
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS products (
    sku               TEXT    PRIMARY KEY,          -- e.g. WIRE-1.5-RED-FINOLEX
    name              TEXT    NOT NULL,             -- name shown to people
    category          TEXT    NOT NULL,             -- wire, switch, mcb, ... (see domain/catalog_terms.py)
    brand             TEXT,
    spec_json         TEXT    NOT NULL DEFAULT '{}',-- {"size_sqmm":1.5,"color":"red"}
    unit              TEXT    NOT NULL,             -- unit the shop SELLS in: coil, nos, box, pcs, metre
    pack_size         INTEGER NOT NULL DEFAULT 1,   -- sell units in one supplier pack (box of 10 -> 10)
    base_price_paise  INTEGER NOT NULL CHECK (base_price_paise >= 0),
    safety_stock      INTEGER NOT NULL DEFAULT 0 CHECK (safety_stock >= 0),
    is_default_brand  INTEGER NOT NULL DEFAULT 0,   -- used when a chitti line has no brand
    active            INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_products_category ON products(category);

-- Local names people write on chittis ("mcb 32 amp", "fin 1.5 red").
CREATE TABLE IF NOT EXISTS product_aliases (
    alias  TEXT NOT NULL,                       -- stored lower-case, single spaces
    sku    TEXT NOT NULL REFERENCES products(sku),
    PRIMARY KEY (alias, sku)
);

-- Generic pricing: base price on the product, optional override per dealer.
CREATE TABLE IF NOT EXISTS dealer_prices (
    dealer_id    INTEGER NOT NULL REFERENCES dealers(id),
    sku          TEXT    NOT NULL REFERENCES products(sku),
    price_paise  INTEGER NOT NULL CHECK (price_paise >= 0),
    PRIMARY KEY (dealer_id, sku)
);

-- -----------------------------------------------------------------------------
-- Stock: append-only movements + reservations
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS stock_movements (
    id          INTEGER PRIMARY KEY,
    sku         TEXT    NOT NULL REFERENCES products(sku),
    qty_delta   INTEGER NOT NULL CHECK (qty_delta <> 0),   -- + in, - out
    reason      TEXT    NOT NULL
                CHECK (reason IN ('opening', 'stock_in', 'order_deduct', 'adjustment')),
    ref_type    TEXT,                                      -- 'order', 'supplier_invoice', 'count'
    ref_id      INTEGER,
    actor       TEXT    NOT NULL,                          -- 'system' or 'staff:<telegram id>'
    note        TEXT,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_moves_sku ON stock_movements(sku);
-- One order can deduct one SKU only once. A retried approval cannot deduct twice.
CREATE UNIQUE INDEX IF NOT EXISTS ux_moves_order_deduct
    ON stock_movements(ref_id, sku) WHERE reason = 'order_deduct';

-- A reservation holds stock for a confirmed order until the owner decides.
CREATE TABLE IF NOT EXISTS reservations (
    id          INTEGER PRIMARY KEY,
    order_id    INTEGER NOT NULL REFERENCES orders(id),
    sku         TEXT    NOT NULL REFERENCES products(sku),
    qty         INTEGER NOT NULL CHECK (qty > 0),
    status      TEXT    NOT NULL CHECK (status IN ('active', 'consumed', 'released')),
    created_at  TEXT    NOT NULL,
    closed_at   TEXT,
    UNIQUE (order_id, sku)
);
CREATE INDEX IF NOT EXISTS ix_res_active ON reservations(sku) WHERE status = 'active';

-- -----------------------------------------------------------------------------
-- Orders
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS orders (
    id                     INTEGER PRIMARY KEY,
    dealer_id              INTEGER NOT NULL REFERENCES dealers(id),
    state                  TEXT    NOT NULL,      -- see domain/states.py
    source_kind            TEXT    NOT NULL CHECK (source_kind IN ('photo', 'text')),
    source_ref             TEXT    NOT NULL,      -- Telegram message ID or upload ID
    idempotency_key        TEXT    NOT NULL UNIQUE,
    image_sha256           TEXT,                  -- for "same file sent two times"
    dealer_check_deadline  TEXT,                  -- AWAITING_DEALER expires after this
    state_reason           TEXT,                  -- why it was rejected / cancelled / expired
    linked_order_id        INTEGER REFERENCES orders(id), -- "add 2 more" after approval -> new linked order
    possible_duplicate_of  INTEGER REFERENCES orders(id), -- similar items from the same dealer, recently
    owner_note             TEXT,
    version                INTEGER NOT NULL DEFAULT 1,  -- +1 on each change (optimistic check)
    created_at             TEXT    NOT NULL,
    updated_at             TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_orders_dealer_state ON orders(dealer_id, state);
CREATE INDEX IF NOT EXISTS ix_orders_state ON orders(state);

CREATE TABLE IF NOT EXISTS order_lines (
    id               INTEGER PRIMARY KEY,
    order_id         INTEGER NOT NULL REFERENCES orders(id),
    line_index       INTEGER NOT NULL,          -- 1, 2, 3 ... top to bottom on the chitti
    raw_text         TEXT    NOT NULL,          -- exactly as read from the chitti
    item_json        TEXT    NOT NULL DEFAULT '{}', -- category/spec/brand as read
    sku              TEXT    REFERENCES products(sku),
    qty              INTEGER,                   -- in the product's sell unit
    unit             TEXT,
    -- ok        : matched, quantity known, stock available
    -- assumed   : matched, but the shop chose something (default brand) - dealer can change
    -- unclear   : the reader or the matcher is not sure; the dealer must answer
    -- unmatched : not in the catalog
    -- short     : not enough sellable stock for the full quantity
    -- removed   : the dealer removed the line
    status           TEXT    NOT NULL
                     CHECK (status IN ('ok', 'assumed', 'unclear', 'unmatched', 'short', 'removed')),
    note             TEXT,                      -- the reason, shown in the read-back
    candidates_json  TEXT    NOT NULL DEFAULT '[]', -- other possible SKUs for an unclear line
    unit_price_paise INTEGER,                   -- price fixed at approval
    UNIQUE (order_id, line_index)
);

-- History of every state change. Also the audit trail for orders.
CREATE TABLE IF NOT EXISTS order_events (
    id          INTEGER PRIMARY KEY,
    order_id    INTEGER NOT NULL REFERENCES orders(id),
    from_state  TEXT,
    to_state    TEXT    NOT NULL,
    actor       TEXT    NOT NULL,     -- 'system', 'dealer:<id>', 'staff:<id>'
    detail_json TEXT    NOT NULL DEFAULT '{}',
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_order ON order_events(order_id);

-- -----------------------------------------------------------------------------
-- Credit ledger
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS bills (
    id            INTEGER PRIMARY KEY,
    dealer_id     INTEGER NOT NULL REFERENCES dealers(id),
    order_id      INTEGER UNIQUE REFERENCES orders(id),   -- NULL for opening balances
    amount_paise  INTEGER NOT NULL CHECK (amount_paise >= 0),
    issued_at     TEXT    NOT NULL,
    due_at        TEXT    NOT NULL,
    paid_at       TEXT,
    paid_paise    INTEGER NOT NULL DEFAULT 0,
    discount_paise INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_bills_dealer ON bills(dealer_id);

-- Cash discount: pay within N days of the bill date, get P percent off.
CREATE TABLE IF NOT EXISTS discount_tiers (
    within_days     INTEGER PRIMARY KEY,
    percent_bp      INTEGER NOT NULL      -- basis points: 300 = 3.00 %
);

-- -----------------------------------------------------------------------------
-- Stock-in from manufacturer invoices
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS supplier_invoices (
    id            INTEGER PRIMARY KEY,
    supplier      TEXT,                    -- NULL while the draft is incomplete
    invoice_no    TEXT,                    -- NULL while the draft is incomplete
    invoice_date  TEXT,
    state         TEXT    NOT NULL CHECK (state IN ('draft', 'applied', 'discarded')),
    source_ref    TEXT    NOT NULL,
    created_by    TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    applied_at    TEXT,
    UNIQUE (supplier, invoice_no)          -- the same invoice cannot be added two times
);

CREATE TABLE IF NOT EXISTS supplier_invoice_lines (
    id           INTEGER PRIMARY KEY,
    invoice_id   INTEGER NOT NULL REFERENCES supplier_invoices(id),
    line_index   INTEGER NOT NULL,
    raw_text     TEXT    NOT NULL,
    item_json    TEXT    NOT NULL DEFAULT '{}',
    sku          TEXT    REFERENCES products(sku),
    packs        INTEGER,                  -- as written on the invoice (boxes, coils)
    sell_units   INTEGER,                  -- packs x product.pack_size
    status       TEXT    NOT NULL CHECK (status IN ('ok', 'unclear', 'unmatched', 'removed')),
    note         TEXT,
    UNIQUE (invoice_id, line_index)
);

-- -----------------------------------------------------------------------------
-- Reliability: uploads, idempotency, outbox, audit
-- -----------------------------------------------------------------------------

-- Images that reached the server (from Telegram or the upload endpoint).
CREATE TABLE IF NOT EXISTS uploads (
    id          TEXT    PRIMARY KEY,        -- random ID, e.g. up_4f1c...
    sha256      TEXT    NOT NULL,
    path        TEXT    NOT NULL,
    mime        TEXT    NOT NULL,
    bytes       INTEGER NOT NULL,
    source      TEXT    NOT NULL,           -- 'telegram' or 'upload'
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_uploads_sha ON uploads(sha256);

-- A tool call with the same key returns the first result again; it does not run twice.
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key          TEXT PRIMARY KEY,
    result_json  TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

-- Transactional outbox: messages are written in the SAME transaction as the
-- state change. A separate worker sends them. A crash cannot lose a message.
CREATE TABLE IF NOT EXISTS outbox (
    id               INTEGER PRIMARY KEY,
    bot              TEXT    NOT NULL CHECK (bot IN ('dealer', 'shop')),
    chat_id          INTEGER NOT NULL,
    kind             TEXT    NOT NULL CHECK (kind IN ('text', 'document', 'photo')),
    payload_json     TEXT    NOT NULL,      -- {"text": ...} or {"path": ..., "caption": ...}
    dedupe_key       TEXT    NOT NULL UNIQUE,
    status           TEXT    NOT NULL CHECK (status IN ('pending', 'sending', 'sent', 'failed')),
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  TEXT    NOT NULL,
    lease_until      TEXT,                  -- worker "owns" the row until this time
    last_error       TEXT,
    created_at       TEXT    NOT NULL,
    sent_at          TEXT
);
CREATE INDEX IF NOT EXISTS ix_outbox_due ON outbox(status, next_attempt_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    detail_json  TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL
);

-- =============================================================================
-- VIEWS. The only objects that text-to-SQL may read.
-- =============================================================================

DROP VIEW IF EXISTS v_stock;
CREATE VIEW v_stock AS
SELECT
    p.sku,
    p.name,
    p.category,
    p.brand,
    p.unit,
    p.safety_stock,
    COALESCE(m.on_hand, 0)                                         AS on_hand,
    COALESCE(r.reserved, 0)                                        AS reserved,
    MAX(COALESCE(m.on_hand, 0) - COALESCE(r.reserved, 0) - p.safety_stock, 0) AS sellable,
    CASE WHEN COALESCE(m.on_hand, 0) - COALESCE(r.reserved, 0) <= p.safety_stock
         THEN 1 ELSE 0 END                                         AS is_low
FROM products p
LEFT JOIN (SELECT sku, SUM(qty_delta) AS on_hand FROM stock_movements GROUP BY sku) m
       ON m.sku = p.sku
LEFT JOIN (SELECT sku, SUM(qty) AS reserved FROM reservations WHERE status = 'active' GROUP BY sku) r
       ON r.sku = p.sku
WHERE p.active = 1;

DROP VIEW IF EXISTS v_orders;
CREATE VIEW v_orders AS
SELECT
    o.id            AS order_id,
    o.state,
    d.name          AS dealer_name,
    d.shop_name     AS dealer_shop,
    o.source_kind,
    o.created_at,
    o.updated_at,
    substr(o.created_at, 1, 10) AS created_date,
    (SELECT COUNT(*) FROM order_lines l WHERE l.order_id = o.id AND l.status <> 'removed') AS line_count,
    b.amount_paise / 100.0 AS bill_amount_rupees
FROM orders o
JOIN dealers d ON d.id = o.dealer_id
LEFT JOIN bills b ON b.order_id = o.id;

DROP VIEW IF EXISTS v_order_lines;
CREATE VIEW v_order_lines AS
SELECT
    l.order_id,
    o.state AS order_state,
    l.line_index,
    l.raw_text,
    l.sku,
    p.name  AS product_name,
    p.category,
    p.brand,
    l.qty,
    l.unit,
    l.status AS line_status,
    l.unit_price_paise / 100.0 AS unit_price_rupees
FROM order_lines l
JOIN orders o ON o.id = l.order_id
LEFT JOIN products p ON p.sku = l.sku;

DROP VIEW IF EXISTS v_dealer_credit;
CREATE VIEW v_dealer_credit AS
SELECT
    d.id AS dealer_id,
    d.name AS dealer_name,
    d.shop_name AS dealer_shop,
    d.status AS dealer_status,
    COALESCE(SUM(CASE WHEN b.paid_at IS NULL THEN b.amount_paise - b.paid_paise END), 0) / 100.0
        AS outstanding_rupees,
    COALESCE(SUM(CASE WHEN b.paid_at IS NULL AND b.due_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                      THEN 1 END), 0) AS overdue_bills,
    MIN(CASE WHEN b.paid_at IS NULL AND b.due_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
             THEN b.due_at END) AS oldest_overdue_due_at
FROM dealers d
LEFT JOIN bills b ON b.dealer_id = d.id
GROUP BY d.id;

DROP VIEW IF EXISTS v_sales_daily;
CREATE VIEW v_sales_daily AS
SELECT
    substr(b.issued_at, 1, 10) AS sale_date,
    COUNT(*)                   AS bills,
    SUM(b.amount_paise) / 100.0 AS amount_rupees
FROM bills b
WHERE b.order_id IS NOT NULL
GROUP BY substr(b.issued_at, 1, 10);
