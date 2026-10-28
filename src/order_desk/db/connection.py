"""SQLite connections and transactions.

Rules this module enforces
--------------------------
1. Every write happens inside `db.write()`, which starts with BEGIN IMMEDIATE.
   BEGIN IMMEDIATE takes SQLite's write lock at the START of the transaction.
   Two writers can never interleave, so two dealers cannot both reserve the
   last 5 units. This does the same job as a Postgres advisory lock
   (pg_advisory_xact_lock) does in a Postgres design.
2. A write transaction must stay short. NEVER call the network (model,
   Telegram) inside `db.write()`. Read, call the network, then write again.
3. Text-to-SQL uses `db.read_only()`, a separate connection that SQLite itself
   opens in read-only mode. See services/reports.py for the second guard.

Each call opens a new connection. SQLite connections are cheap, and this keeps
the code safe when the MCP server runs tools in worker threads.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = "1"


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    # ------------------------------------------------------------------ setup
    def initialize(self) -> None:
        """Create the file, the tables and the views (safe to run again)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        schema = resources.files("order_desk.db").joinpath("schema.sql").read_text("utf-8")
        with self._open() as conn:
            conn.execute("PRAGMA journal_mode = WAL")   # readers do not block the writer
            conn.executescript(schema)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (SCHEMA_VERSION,),
            )

    # ------------------------------------------------------------ connections
    def _open(self, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            uri = f"file:{self.path.resolve()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=10)
        else:
            conn = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        """A connection for plain reads (no transaction)."""
        conn = self._open()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """One write transaction. Commits on success, rolls back on any error."""
        conn = self._open()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextmanager
    def read_only(self) -> Iterator[sqlite3.Connection]:
        """A connection that SQLite opens read-only. Used for text-to-SQL."""
        conn = self._open(read_only=True)
        conn.execute("PRAGMA query_only = ON")
        try:
            yield conn
        finally:
            conn.close()
