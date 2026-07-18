from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = ROOT / "data" / "app.db"
Backend = Literal["sqlite", "supabase"]
TABLES = {
    "accounts",
    "transactions",
    "dividends",
    "portfolio_snapshots",
    "net_worth_history",
    "prices",
    "app_metadata",
}

_SUPABASE_FALLBACK_REASON = ""
_SUPABASE_READY = False
_SQLITE_READY_PATH = ""
logger = logging.getLogger(__name__)


def db_path() -> Path:
    configured = os.getenv("ASSET_DB_PATH", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_DB_PATH


def supabase_url() -> str:
    return (
        os.getenv("SUPABASE_DB_URL", "").strip()
        or os.getenv("SUPABASE_DATABASE_URL", "").strip()
        or os.getenv("DATABASE_URL", "").strip()
    )


def supabase_components(url: str | None = None) -> dict[str, Any]:
    raw_url = url if url is not None else supabase_url()
    if not raw_url:
        return {"hasSupabaseUrl": False, "host": "", "database": "", "sslmode": ""}

    parsed = urlparse(raw_url)
    query = parse_qs(parsed.query)
    return {
        "hasSupabaseUrl": True,
        "host": parsed.hostname or "",
        "database": parsed.path.lstrip("/"),
        "sslmode": (query.get("sslmode") or [""])[0],
    }


def supabase_connection_url() -> str:
    raw_url = supabase_url()
    if not raw_url:
        return ""

    parsed = urlparse(raw_url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query.setdefault("sslmode", ["require"])
    query.setdefault("connect_timeout", ["6"])
    query.setdefault("options", ["-c statement_timeout=8000 -c lock_timeout=5000"])
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def encode(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def decode(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


def _connect_sqlite() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _connect_postgres():
    import psycopg
    from psycopg.rows import dict_row

    connection = psycopg.connect(
        supabase_connection_url(),
        row_factory=dict_row,
        connect_timeout=6,
        prepare_threshold=None,
    )
    connection.execute("SET statement_timeout TO '8000ms'")
    connection.execute("SET lock_timeout TO '5000ms'")
    return connection


def _sqlite_script() -> str:
    return """
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
        amount REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'completed' CHECK (status IN ('pending', 'completed')),
        created_at TEXT,
        settle_at TEXT,
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

    CREATE TABLE IF NOT EXISTS app_metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """


def _postgres_statements() -> list[str]:
    return [
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id TEXT PRIMARY KEY,
            date TEXT NOT NULL,
            market TEXT NOT NULL,
            symbol TEXT NOT NULL,
            amount DOUBLE PRECISION NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'completed' CHECK (status IN ('pending', 'completed')),
            created_at TIMESTAMPTZ,
            settle_at TIMESTAMPTZ,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS amount DOUBLE PRECISION NOT NULL DEFAULT 0",
        "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'completed'",
        "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ",
        "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS settle_at TIMESTAMPTZ",
        "CREATE INDEX IF NOT EXISTS idx_transactions_pending_settlement ON transactions (status, settle_at)",
        """
        CREATE TABLE IF NOT EXISTS dividends (
            id TEXT PRIMARY KEY,
            date TEXT NOT NULL,
            market TEXT NOT NULL,
            symbol TEXT NOT NULL,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS portfolio_snapshots (
            id BIGSERIAL PRIMARY KEY,
            created_at TEXT NOT NULL,
            payload TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS net_worth_history (
            date TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS prices (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            payload TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS app_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
    ]


def _postgres_security_statements() -> list[str]:
    statements: list[str] = []
    for table in sorted(TABLES):
        statements.extend(
            [
                f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY",
                f"REVOKE ALL ON TABLE public.{table} FROM anon, authenticated",
                f"GRANT ALL ON TABLE public.{table} TO CURRENT_USER",
                f"DROP POLICY IF EXISTS dashboard_backend_all ON public.{table}",
                (
                    f"CREATE POLICY dashboard_backend_all ON public.{table} "
                    "FOR ALL TO public USING (true) WITH CHECK (true)"
                ),
            ]
        )
    statements.append("GRANT USAGE, SELECT ON SEQUENCE public.portfolio_snapshots_id_seq TO CURRENT_USER")
    return statements


def _mark_supabase_failed(error: Exception) -> None:
    global _SUPABASE_FALLBACK_REASON, _SUPABASE_READY
    _SUPABASE_FALLBACK_REASON = str(error)
    _SUPABASE_READY = False
    details = supabase_components(supabase_connection_url())
    logger.exception(
        "Supabase connection failed; falling back to SQLite. host=%s database=%s sslmode=%s errorType=%s errorMessage=%s",
        details["host"] or "(empty)",
        details["database"] or "(empty)",
        details["sslmode"] or "(empty)",
        type(error).__name__,
        str(error),
    )


def reset_supabase_fallback() -> None:
    global _SUPABASE_FALLBACK_REASON, _SUPABASE_READY
    _SUPABASE_FALLBACK_REASON = ""
    _SUPABASE_READY = False


def debug_supabase() -> dict[str, Any]:
    details = supabase_components(supabase_connection_url() or supabase_url())
    error_type = ""
    error_message = ""

    if not details["hasSupabaseUrl"]:
        return {**details, "errorType": "MissingSupabaseUrl", "errorMessage": "SUPABASE_DB_URL is not configured."}

    try:
        import psycopg  # noqa: F401
        reset_supabase_fallback()
        init_supabase()
    except Exception as error:
        _mark_supabase_failed(error)
        error_type = type(error).__name__
        error_message = str(error)
    else:
        logger.info(
            "Supabase connection succeeded. host=%s database=%s sslmode=%s",
            details["host"] or "(empty)",
            details["database"] or "(empty)",
            details["sslmode"] or "(empty)",
        )

    return {**details, "errorType": error_type, "errorMessage": error_message}


def init_sqlite() -> None:
    global _SQLITE_READY_PATH
    path = str(db_path())
    if _SQLITE_READY_PATH == path:
        return
    with _connect_sqlite() as connection:
        connection.executescript(_sqlite_script())
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(transactions)")}
        migrations = {
            "amount": "ALTER TABLE transactions ADD COLUMN amount REAL NOT NULL DEFAULT 0",
            "status": "ALTER TABLE transactions ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'",
            "created_at": "ALTER TABLE transactions ADD COLUMN created_at TEXT",
            "settle_at": "ALTER TABLE transactions ADD COLUMN settle_at TEXT",
        }
        for column, statement in migrations.items():
            if column not in columns:
                connection.execute(statement)
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_transactions_pending_settlement ON transactions (status, settle_at)"
        )
        connection.commit()
    _SQLITE_READY_PATH = path


def init_supabase() -> None:
    global _SUPABASE_FALLBACK_REASON, _SUPABASE_READY
    if _SUPABASE_READY:
        return
    with _connect_postgres() as connection:
        with connection.cursor() as cursor:
            for statement in _postgres_statements():
                cursor.execute(statement)
            for statement in _postgres_security_statements():
                cursor.execute(statement)
        connection.commit()
    _SUPABASE_READY = True
    _SUPABASE_FALLBACK_REASON = ""


def init_db() -> None:
    init_sqlite()
    if not supabase_url():
        return
    try:
        init_supabase()
    except Exception as error:
        _mark_supabase_failed(error)


def active_backend() -> Backend:
    if not supabase_url():
        init_sqlite()
        return "sqlite"
    if _SUPABASE_READY:
        return "supabase"
    try:
        init_supabase()
        return "supabase"
    except Exception as error:
        _mark_supabase_failed(error)
        init_sqlite()
        return "sqlite"


def current_db_label() -> str:
    return "Supabase PostgreSQL" if active_backend() == "supabase" else "SQLite"


def connect():
    backend = active_backend()
    return _connect_postgres() if backend == "supabase" else _connect_sqlite()


def _placeholder(backend: Backend) -> str:
    return "%s" if backend == "supabase" else "?"


def _row_value(row: Any, key: str) -> Any:
    return row[key]


def _using_supabase_fallback(backend: Backend) -> bool:
    return backend == "sqlite" and bool(supabase_url()) and bool(_SUPABASE_FALLBACK_REASON)


def _raise_supabase_write_error(error: Exception | None = None) -> None:
    message = "Supabase 目前無法寫入，資料未保存。請稍後再試，或到設定頁確認 Database Health。"
    if error is None:
        raise RuntimeError(message)
    raise RuntimeError(f"{message} 原因：{error}") from error


def _execute_read(sqlite_sql: str, postgres_sql: str | None = None, params: tuple[Any, ...] = ()):
    backend = active_backend()
    query = postgres_sql if backend == "supabase" and postgres_sql else sqlite_sql
    try:
        with connect() as connection:
            cursor = connection.execute(query, params) if backend == "sqlite" else connection.cursor()
            if backend == "supabase":
                cursor.execute(query, params)
            rows = cursor.fetchall()
        return rows, backend
    except Exception as error:
        if backend == "supabase":
            _mark_supabase_failed(error)
            init_sqlite()
            with _connect_sqlite() as connection:
                rows = connection.execute(sqlite_sql, params).fetchall()
            return rows, "sqlite"
        raise


def _execute_write(sqlite_sql: str, postgres_sql: str | None = None, params: tuple[Any, ...] = ()) -> Backend:
    backend = active_backend()
    if _using_supabase_fallback(backend):
        _raise_supabase_write_error()
    query = postgres_sql if backend == "supabase" and postgres_sql else sqlite_sql
    try:
        with connect() as connection:
            if backend == "sqlite":
                connection.execute(query, params)
            else:
                with connection.cursor() as cursor:
                    cursor.execute(query, params)
            connection.commit()
        return backend
    except Exception as error:
        if backend == "supabase":
            _mark_supabase_failed(error)
            _raise_supabase_write_error(error)
        raise


def _execute_many(sqlite_sql: str, postgres_sql: str | None, rows: list[tuple[Any, ...]]) -> Backend:
    backend = active_backend()
    if _using_supabase_fallback(backend):
        _raise_supabase_write_error()
    query = postgres_sql if backend == "supabase" and postgres_sql else sqlite_sql
    try:
        with connect() as connection:
            if backend == "sqlite":
                connection.executemany(query, rows)
            else:
                with connection.cursor() as cursor:
                    cursor.executemany(query, rows)
            connection.commit()
        return backend
    except Exception as error:
        if backend == "supabase":
            _mark_supabase_failed(error)
            _raise_supabase_write_error(error)
        raise


def _replace_rows(table: str, sqlite_insert: str, postgres_insert: str | None, rows: list[tuple[Any, ...]]) -> Backend:
    if table not in TABLES:
        raise ValueError(f"Unknown table: {table}")
    backend = active_backend()
    if _using_supabase_fallback(backend):
        _raise_supabase_write_error()
    insert = postgres_insert if backend == "supabase" and postgres_insert else sqlite_insert
    try:
        with connect() as connection:
            if backend == "sqlite":
                connection.execute(f"DELETE FROM {table}")
                if rows:
                    connection.executemany(insert, rows)
            else:
                with connection.cursor() as cursor:
                    cursor.execute(f"DELETE FROM {table}")
                    if rows:
                        cursor.executemany(insert, rows)
            connection.commit()
        return backend
    except Exception as error:
        if backend == "supabase":
            _mark_supabase_failed(error)
            _raise_supabase_write_error(error)
        raise


def db_exists() -> bool:
    return db_path().exists()


def table_count(table: str) -> int:
    if table not in TABLES:
        raise ValueError(f"Unknown table: {table}")
    rows, _ = _execute_read(f"SELECT COUNT(*) AS count FROM {table}")
    return int(_row_value(rows[0], "count")) if rows else 0


def table_counts() -> dict[str, int]:
    rows, _ = _execute_read(
        """
        SELECT
          (SELECT COUNT(*) FROM accounts) AS accounts,
          (SELECT COUNT(*) FROM transactions) AS transactions,
          (SELECT COUNT(*) FROM dividends) AS dividends,
          (SELECT COUNT(*) FROM portfolio_snapshots) AS portfolio_snapshots,
          (SELECT COUNT(*) FROM net_worth_history) AS net_worth_history,
          (SELECT COUNT(*) FROM prices) AS prices
        """
    )
    row = rows[0] if rows else {}
    return {
        "accounts": int(_row_value(row, "accounts")) if row else 0,
        "transactions": int(_row_value(row, "transactions")) if row else 0,
        "dividends": int(_row_value(row, "dividends")) if row else 0,
        "portfolioSnapshots": int(_row_value(row, "portfolio_snapshots")) if row else 0,
        "netWorthHistory": int(_row_value(row, "net_worth_history")) if row else 0,
        "prices": int(_row_value(row, "prices")) if row else 0,
    }


def read_metadata() -> dict[str, Any]:
    rows, _ = _execute_read("SELECT key, value FROM app_metadata WHERE key <> 'financeData' ORDER BY key")
    output: dict[str, Any] = {}
    for row in rows:
        key = _row_value(row, "key")
        output[key] = decode(_row_value(row, "value"), None)
    return output


def set_metadata(key: str, value: Any) -> None:
    timestamp = now_iso()
    params = (key, encode(value), timestamp)
    _execute_write(
        """
        INSERT INTO app_metadata (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        """
        INSERT INTO app_metadata (key, value, updated_at)
        VALUES (%s, %s, %s)
        ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
        """,
        params,
    )


def read_finance_data(default: dict[str, Any] | None = None) -> dict[str, Any]:
    rows, _ = _execute_read("SELECT value FROM app_metadata WHERE key = 'financeData'")
    return decode(_row_value(rows[0], "value"), default or {}) if rows else (default or {})


def write_finance_data(finance_data: dict[str, Any]) -> Backend:
    timestamp = now_iso()
    return _execute_write(
        """
        INSERT INTO app_metadata (key, value, updated_at)
        VALUES ('financeData', ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        """
        INSERT INTO app_metadata (key, value, updated_at)
        VALUES ('financeData', %s, %s)
        ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
        """,
        (encode(finance_data), timestamp),
    )


def status() -> dict[str, Any]:
    backend = active_backend()
    path = db_path()
    counts = table_counts()
    has_data = any(counts.values())
    return {
        "ok": True,
        "currentDb": backend,
        "currentDbLabel": "Supabase PostgreSQL" if backend == "supabase" else "SQLite",
        "dataStatus": "正式資料" if has_data else "範例資料",
        "fallbackActive": backend == "sqlite" and bool(supabase_url()) and bool(_SUPABASE_FALLBACK_REASON),
        "fallbackReason": _SUPABASE_FALLBACK_REASON,
        "supabaseConfigured": bool(supabase_url()),
        "dbPath": str(path),
        "dbExists": path.exists(),
        "usingConfiguredPath": bool(os.getenv("ASSET_DB_PATH", "").strip()),
        "counts": counts,
        "metadata": read_metadata(),
    }


def read_accounts(default: dict[str, Any] | None = None) -> dict[str, Any]:
    rows, _ = _execute_read("SELECT payload FROM accounts WHERE id = 1")
    return decode(_row_value(rows[0], "payload"), default or {}) if rows else (default or {})


def write_accounts(accounts: dict[str, Any]) -> None:
    _execute_write(
        """
        INSERT INTO accounts (id, payload, updated_at)
        VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at
        """,
        """
        INSERT INTO accounts (id, payload, updated_at)
        VALUES (1, %s, %s)
        ON CONFLICT(id) DO UPDATE SET payload = EXCLUDED.payload, updated_at = EXCLUDED.updated_at
        """,
        (encode(accounts), now_iso()),
    )


def read_prices(default: dict[str, Any] | None = None) -> dict[str, Any]:
    fallback = default or {"fxRate": 31.451, "prices": {}}
    rows, _ = _execute_read("SELECT payload FROM prices WHERE id = 1")
    return decode(_row_value(rows[0], "payload"), fallback) if rows else fallback


def write_prices(prices: dict[str, Any]) -> Backend:
    return _execute_write(
        """
        INSERT INTO prices (id, payload, updated_at)
        VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at
        """,
        """
        INSERT INTO prices (id, payload, updated_at)
        VALUES (1, %s, %s)
        ON CONFLICT(id) DO UPDATE SET payload = EXCLUDED.payload, updated_at = EXCLUDED.updated_at
        """,
        (encode(prices), now_iso()),
    )


def clear_all() -> None:
    backend = active_backend()
    if _using_supabase_fallback(backend):
        _raise_supabase_write_error()
    try:
        with connect() as connection:
            if backend == "sqlite":
                for table in ["accounts", "transactions", "dividends", "portfolio_snapshots", "net_worth_history", "prices"]:
                    connection.execute(f"DELETE FROM {table}")
            else:
                with connection.cursor() as cursor:
                    for table in ["accounts", "transactions", "dividends", "portfolio_snapshots", "net_worth_history", "prices"]:
                        cursor.execute(f"DELETE FROM {table}")
            connection.commit()
    except Exception as error:
        if backend != "supabase":
            raise
        _mark_supabase_failed(error)
        _raise_supabase_write_error(error)


def read_transactions() -> list[dict[str, Any]]:
    rows, _ = _execute_read(
        "SELECT payload, amount, status, created_at, settle_at FROM transactions ORDER BY date, id"
    )
    output = []
    for row in rows:
        payload = decode(_row_value(row, "payload"), {})
        payload.update({
            "amount": float(_row_value(row, "amount") or 0),
            "status": str(_row_value(row, "status") or "completed"),
            "createdAt": _row_value(row, "created_at"),
            "settleAt": _row_value(row, "settle_at"),
        })
        output.append(payload)
    return output


def write_transactions(transactions: list[dict[str, Any]]) -> None:
    timestamp = now_iso()
    rows = [
        (
            str(row["id"]),
            str(row.get("date", "")),
            str(row.get("market", "")).upper(),
            str(row.get("symbol", "")).upper(),
            float(row.get("amount", 0) or 0),
            str(row.get("status", "completed")),
            row.get("createdAt"),
            row.get("settleAt"),
            encode(row),
            timestamp,
        )
        for row in transactions
    ]
    _replace_rows(
        "transactions",
        "INSERT INTO transactions (id, date, market, symbol, amount, status, created_at, settle_at, payload, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        "INSERT INTO transactions (id, date, market, symbol, amount, status, created_at, settle_at, payload, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        rows,
    )


def settle_expired_transactions_and_sum_pending(now: datetime | None = None) -> float:
    """Atomically expire T+2 rows, then aggregate the remaining cash outflow."""
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    timestamp = current.isoformat(timespec="seconds")
    backend = active_backend()
    # A read-only fallback must still render the correct available cash. Once the
    # primary database reconnects, the normal request persists completed states.
    if not _using_supabase_fallback(backend):
        _execute_write(
            "UPDATE transactions SET status = 'completed', updated_at = ? WHERE status = 'pending' AND settle_at IS NOT NULL AND settle_at <= ?",
            "UPDATE transactions SET status = 'completed', updated_at = %s WHERE status = 'pending' AND settle_at IS NOT NULL AND settle_at <= %s::timestamptz",
            (timestamp, timestamp),
        )
    rows, _ = _execute_read(
        "SELECT COALESCE(SUM(amount), 0) AS pending_settlement FROM transactions WHERE status = 'pending' AND settle_at IS NOT NULL AND settle_at > ?",
        "SELECT COALESCE(SUM(amount), 0) AS pending_settlement FROM transactions WHERE status = 'pending' AND settle_at IS NOT NULL AND settle_at > %s::timestamptz",
        (timestamp,),
    )
    value = float(_row_value(rows[0], "pending_settlement") or 0) if rows else 0.0
    return max(0.0, value)


def read_dividends() -> list[dict[str, Any]]:
    rows, _ = _execute_read("SELECT payload FROM dividends ORDER BY date, id")
    return [decode(_row_value(row, "payload"), {}) for row in rows]


def write_dividends(dividends: list[dict[str, Any]]) -> None:
    timestamp = now_iso()
    rows = [
        (
            str(row["id"]),
            str(row.get("date", "")),
            str(row.get("market", "")).upper(),
            str(row.get("symbol", "")).upper(),
            encode(row),
            timestamp,
        )
        for row in dividends
    ]
    _replace_rows(
        "dividends",
        "INSERT INTO dividends (id, date, market, symbol, payload, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        "INSERT INTO dividends (id, date, market, symbol, payload, updated_at) VALUES (%s, %s, %s, %s, %s, %s)",
        rows,
    )


def upsert_dividend(dividend: dict[str, Any]) -> Backend:
    timestamp = now_iso()
    row = (
        str(dividend["id"]),
        str(dividend.get("date", "")),
        str(dividend.get("market", "")).upper(),
        str(dividend.get("symbol", "")).upper(),
        encode(dividend),
        timestamp,
    )
    return _execute_write(
        """
        INSERT INTO dividends (id, date, market, symbol, payload, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            date = excluded.date,
            market = excluded.market,
            symbol = excluded.symbol,
            payload = excluded.payload,
            updated_at = excluded.updated_at
        """,
        """
        INSERT INTO dividends (id, date, market, symbol, payload, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT(id) DO UPDATE SET
            date = EXCLUDED.date,
            market = EXCLUDED.market,
            symbol = EXCLUDED.symbol,
            payload = EXCLUDED.payload,
            updated_at = EXCLUDED.updated_at
        """,
        row,
    )


def delete_dividend(dividend_id: str) -> Backend:
    return _execute_write(
        "DELETE FROM dividends WHERE id = ?",
        "DELETE FROM dividends WHERE id = %s",
        (dividend_id,),
    )


def clear_table(table: str) -> None:
    if table not in TABLES:
        raise ValueError(f"Unknown table: {table}")
    _execute_write(f"DELETE FROM {table}")


def read_latest_portfolio(default: dict[str, Any] | None = None) -> dict[str, Any]:
    rows, _ = _execute_read("SELECT payload FROM portfolio_snapshots ORDER BY id DESC LIMIT 1")
    return decode(_row_value(rows[0], "payload"), default or {}) if rows else (default or {})


def write_portfolio_snapshot(portfolio: dict[str, Any]) -> None:
    _execute_write(
        "INSERT INTO portfolio_snapshots (created_at, payload) VALUES (?, ?)",
        "INSERT INTO portfolio_snapshots (created_at, payload) VALUES (%s, %s)",
        (now_iso(), encode(portfolio)),
    )


def replace_portfolio_snapshot(portfolio: dict[str, Any]) -> None:
    clear_table("portfolio_snapshots")
    write_portfolio_snapshot(portfolio)


def read_net_worth_history() -> list[dict[str, Any]]:
    rows, _ = _execute_read("SELECT payload FROM net_worth_history ORDER BY date")
    return [decode(_row_value(row, "payload"), {}) for row in rows]


def write_net_worth_history(history: list[dict[str, Any]]) -> None:
    timestamp = now_iso()
    by_date: dict[str, dict[str, Any]] = {}
    for row in history:
        history_date = str(row.get("date", "")).strip()
        if history_date:
            by_date[history_date] = {**row, "date": history_date}
    rows = [(history_date, encode(row), timestamp) for history_date, row in sorted(by_date.items())]
    _replace_rows(
        "net_worth_history",
        "INSERT INTO net_worth_history (date, payload, updated_at) VALUES (?, ?, ?)",
        "INSERT INTO net_worth_history (date, payload, updated_at) VALUES (%s, %s, %s)",
        rows,
    )


def has_private_data() -> bool:
    counts = status()["counts"]
    return any(
        counts[key] > 0
        for key in ["accounts", "transactions", "dividends", "portfolioSnapshots", "netWorthHistory", "prices"]
    )


def export_backup() -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "exportedAt": now_iso(),
        "sourceDb": active_backend(),
        "accounts": read_accounts({}),
        "transactions": read_transactions(),
        "dividends": read_dividends(),
        "prices": read_prices({"fxRate": 31.451, "prices": {}}),
        "net-worth-history": read_net_worth_history(),
        "portfolio": read_latest_portfolio({}),
        "financeData": read_finance_data({}),
    }


def import_backup_payload(payload: dict[str, Any]) -> dict[str, Any]:
    clear_all()
    imported: list[str] = []

    accounts = payload.get("accounts")
    if isinstance(accounts, dict):
        write_accounts(accounts)
        imported.append("accounts")

    transactions = payload.get("transactions")
    if isinstance(transactions, list):
        write_transactions(transactions)
        imported.append("transactions")

    dividends = payload.get("dividends")
    if isinstance(dividends, list):
        write_dividends(dividends)
        imported.append("dividends")

    prices = payload.get("prices")
    if isinstance(prices, dict):
        write_prices(prices)
        imported.append("prices")

    history = payload.get("net-worth-history", payload.get("net_worth_history"))
    if isinstance(history, list):
        write_net_worth_history(history)
        imported.append("net-worth-history")

    portfolio = payload.get("portfolio")
    if isinstance(portfolio, dict) and portfolio:
        write_portfolio_snapshot(portfolio)
        imported.append("portfolio")

    finance_data = payload.get("financeData", payload.get("finance-data"))
    if isinstance(finance_data, dict):
        write_finance_data(finance_data)
        imported.append("financeData")

    return {"imported": imported, "counts": status()["counts"]}


def migrate_sqlite_to_supabase() -> dict[str, Any]:
    if not supabase_url():
        raise RuntimeError("尚未設定 SUPABASE_DB_URL。")

    global _SUPABASE_FALLBACK_REASON, _SUPABASE_READY
    _SUPABASE_FALLBACK_REASON = ""
    _SUPABASE_READY = False
    init_supabase()

    active_backup = export_backup()
    sqlite_payload = _export_sqlite_backup()
    if not any(
        sqlite_payload.get(key)
        for key in ["accounts", "transactions", "dividends", "prices", "net-worth-history", "portfolio"]
    ):
        sqlite_payload = active_backup

    _import_payload_to_backend(sqlite_payload, "supabase")
    return {
        "ok": True,
        "currentDb": active_backend(),
        "counts": status()["counts"],
        "message": "SQLite 資料已遷移到 Supabase。",
    }


def _export_sqlite_backup() -> dict[str, Any]:
    original_reason = _SUPABASE_FALLBACK_REASON
    try:
        globals()["_SUPABASE_FALLBACK_REASON"] = "force-sqlite-export"
        return export_backup()
    finally:
        globals()["_SUPABASE_FALLBACK_REASON"] = original_reason


def _import_payload_to_backend(payload: dict[str, Any], backend: Backend) -> None:
    if backend == "supabase":
        with _connect_postgres() as connection:
            with connection.cursor() as cursor:
                for table in ["accounts", "transactions", "dividends", "portfolio_snapshots", "net_worth_history", "prices"]:
                    cursor.execute(f"DELETE FROM {table}")
            connection.commit()
    else:
        clear_all()

    original_reason = _SUPABASE_FALLBACK_REASON
    try:
        if backend == "supabase":
            globals()["_SUPABASE_FALLBACK_REASON"] = ""
        import_backup_payload(payload)
    finally:
        globals()["_SUPABASE_FALLBACK_REASON"] = original_reason
