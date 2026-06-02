from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = ROOT / "data" / "app.db"


def db_path() -> Path:
    configured = os.getenv("ASSET_DB_PATH", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_DB_PATH


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def encode(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def decode(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    init_db(connection)
    return connection


def init_db(connection: sqlite3.Connection | None = None) -> None:
    owns_connection = connection is None
    if connection is None:
        path = db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id TEXT PRIMARY KEY,
            date TEXT NOT NULL,
            market TEXT NOT NULL,
            symbol TEXT NOT NULL,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS dividends (
            id TEXT PRIMARY KEY,
            date TEXT NOT NULL,
            market TEXT NOT NULL,
            symbol TEXT NOT NULL,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            payload TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS net_worth_history (
            date TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS prices (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    connection.commit()
    if owns_connection:
        connection.close()


def db_exists() -> bool:
    return db_path().exists()


def table_count(table: str) -> int:
    with connect() as connection:
        row = connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
    return int(row["count"])


def status() -> dict[str, Any]:
    init_db()
    path = db_path()
    return {
        "ok": True,
        "dbPath": str(path),
        "dbExists": path.exists(),
        "usingConfiguredPath": bool(os.getenv("ASSET_DB_PATH", "").strip()),
        "counts": {
            "accounts": table_count("accounts"),
            "transactions": table_count("transactions"),
            "dividends": table_count("dividends"),
            "portfolioSnapshots": table_count("portfolio_snapshots"),
            "netWorthHistory": table_count("net_worth_history"),
            "prices": table_count("prices"),
        },
    }


def read_accounts(default: dict[str, Any] | None = None) -> dict[str, Any]:
    with connect() as connection:
        row = connection.execute("SELECT payload FROM accounts WHERE id = 1").fetchone()
    return decode(row["payload"], default or {}) if row else (default or {})


def write_accounts(accounts: dict[str, Any]) -> None:
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO accounts (id, payload, updated_at)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at
            """,
            (encode(accounts), now_iso()),
        )
        connection.commit()


def read_prices(default: dict[str, Any] | None = None) -> dict[str, Any]:
    with connect() as connection:
        row = connection.execute("SELECT payload FROM prices WHERE id = 1").fetchone()
    return decode(row["payload"], default or {"fxRate": 31.451, "prices": {}}) if row else (default or {"fxRate": 31.451, "prices": {}})


def write_prices(prices: dict[str, Any]) -> None:
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO prices (id, payload, updated_at)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at
            """,
            (encode(prices), now_iso()),
        )
        connection.commit()


def read_transactions() -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute("SELECT payload FROM transactions ORDER BY date, id").fetchall()
    return [decode(row["payload"], {}) for row in rows]


def write_transactions(transactions: list[dict[str, Any]]) -> None:
    timestamp = now_iso()
    with connect() as connection:
        connection.execute("DELETE FROM transactions")
        connection.executemany(
            """
            INSERT INTO transactions (id, date, market, symbol, payload, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    str(row["id"]),
                    str(row.get("date", "")),
                    str(row.get("market", "")).upper(),
                    str(row.get("symbol", "")).upper(),
                    encode(row),
                    timestamp,
                )
                for row in transactions
            ],
        )
        connection.commit()


def read_dividends() -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute("SELECT payload FROM dividends ORDER BY date, id").fetchall()
    return [decode(row["payload"], {}) for row in rows]


def write_dividends(dividends: list[dict[str, Any]]) -> None:
    timestamp = now_iso()
    with connect() as connection:
        connection.execute("DELETE FROM dividends")
        connection.executemany(
            """
            INSERT INTO dividends (id, date, market, symbol, payload, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    str(row["id"]),
                    str(row.get("date", "")),
                    str(row.get("market", "")).upper(),
                    str(row.get("symbol", "")).upper(),
                    encode(row),
                    timestamp,
                )
                for row in dividends
            ],
        )
        connection.commit()


def read_latest_portfolio(default: dict[str, Any] | None = None) -> dict[str, Any]:
    with connect() as connection:
        row = connection.execute(
            "SELECT payload FROM portfolio_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return decode(row["payload"], default or {}) if row else (default or {})


def write_portfolio_snapshot(portfolio: dict[str, Any]) -> None:
    with connect() as connection:
        connection.execute(
            "INSERT INTO portfolio_snapshots (created_at, payload) VALUES (?, ?)",
            (now_iso(), encode(portfolio)),
        )
        connection.commit()


def read_net_worth_history() -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute("SELECT payload FROM net_worth_history ORDER BY date").fetchall()
    return [decode(row["payload"], {}) for row in rows]


def write_net_worth_history(history: list[dict[str, Any]]) -> None:
    timestamp = now_iso()
    with connect() as connection:
        connection.execute("DELETE FROM net_worth_history")
        connection.executemany(
            """
            INSERT INTO net_worth_history (date, payload, updated_at)
            VALUES (?, ?, ?)
            """,
            [(str(row.get("date", "")), encode(row), timestamp) for row in history if row.get("date")]
        )
        connection.commit()


def has_private_data() -> bool:
    counts = status()["counts"]
    return any(
        counts[key] > 0
        for key in ["accounts", "transactions", "dividends", "portfolioSnapshots", "netWorthHistory", "prices"]
    )
