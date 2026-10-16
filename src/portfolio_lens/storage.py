"""Persist exact transaction values and import history in a local SQLite file."""

import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from portfolio_lens.models import CommitResult, ImportPreview, Transaction


def record_key(record: dict) -> tuple:
    return (
        record["ticker"],
        date.fromisoformat(record["date"]),
        record["transaction_type"],
        Decimal(record["quantity"]),
        Decimal(record["price"]),
        record["broker"].casefold(),
    )


class TransactionStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection, connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS app_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    revision INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO app_state (id, revision) VALUES (1, 0);
                CREATE TABLE IF NOT EXISTS imports (
                    id INTEGER PRIMARY KEY,
                    source_name TEXT NOT NULL,
                    split_convention TEXT NOT NULL
                        CHECK (split_convention IN ('already_adjusted', 'as_traded')),
                    imported_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY,
                    import_id INTEGER NOT NULL REFERENCES imports(id) ON DELETE CASCADE,
                    ticker TEXT NOT NULL,
                    date TEXT NOT NULL,
                    transaction_type TEXT NOT NULL CHECK (transaction_type IN ('BUY', 'SELL')),
                    quantity TEXT NOT NULL,
                    price TEXT NOT NULL,
                    broker TEXT NOT NULL,
                    source_row INTEGER NOT NULL
                );
            """)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _records(connection: sqlite3.Connection) -> list[dict]:
        records = connection.execute("""
            SELECT t.id, t.ticker, t.date, t.transaction_type, t.quantity, t.price,
                   t.broker, t.source_row, i.source_name, i.split_convention, i.imported_at
            FROM transactions AS t JOIN imports AS i ON t.import_id = i.id
            ORDER BY t.date, t.id
        """).fetchall()
        return [dict(record) for record in records]

    def snapshot(self) -> tuple[int, list[dict]]:
        """Read the revision and records from the same database snapshot."""
        with self._connect() as connection, connection:
            connection.execute("BEGIN")
            revision = connection.execute("SELECT revision FROM app_state WHERE id = 1").fetchone()[
                0
            ]
            return revision, self._records(connection)

    def commit(self, preview: ImportPreview, duplicate_policy: str) -> CommitResult:
        """Apply a whole batch atomically, including replacement and duplicate checks."""
        with self._connect() as connection, connection:
            # Serialize writers before checking whether this preview is still current.
            connection.execute("BEGIN IMMEDIATE")
            revision = connection.execute("SELECT revision FROM app_state WHERE id = 1").fetchone()[
                0
            ]
            if revision != preview.storage_revision:
                raise ValueError(
                    "Saved transactions changed after this preview. Preview again before saving."
                )

            records = self._records(connection)
            seen = {record_key(record) for record in records} if preview.mode == "append" else set()
            to_insert: list[Transaction] = []
            skipped = 0
            for transaction in preview.transactions:
                if duplicate_policy == "skip" and transaction.duplicate_key in seen:
                    skipped += 1
                else:
                    to_insert.append(transaction)
                seen.add(transaction.duplicate_key)

            if not to_insert and preview.mode == "append":
                return CommitResult(0, skipped, len(records), preview.mode)

            if preview.mode == "replace":
                connection.execute("DELETE FROM imports")

            imported_at = datetime.now(timezone.utc).isoformat()
            cursor = connection.execute(
                "INSERT INTO imports (source_name, split_convention, imported_at) VALUES (?, ?, ?)",
                (preview.source_name, preview.split_convention, imported_at),
            )
            import_id = cursor.lastrowid
            connection.executemany(
                """
                INSERT INTO transactions (
                    import_id, ticker, date, transaction_type, quantity, price, broker, source_row
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                [
                    (
                        import_id,
                        transaction.ticker,
                        transaction.date.isoformat(),
                        transaction.transaction_type,
                        format(transaction.quantity, "f"),
                        format(transaction.price, "f"),
                        transaction.broker,
                        transaction.source_row,
                    )
                    for transaction in to_insert
                ],
            )
            connection.execute("UPDATE app_state SET revision = revision + 1 WHERE id = 1")
            total = connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
            return CommitResult(len(to_insert), skipped, total, preview.mode)
