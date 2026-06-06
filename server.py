from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import tempfile
from datetime import datetime
from uuid import uuid4
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from scripts.import_moze_csv import import_moze_csv
from scripts import db_store
from scripts.portfolio_core import (
    DATA_DIR,
    TWD,
    build_portfolio_from_data,
    calculate_positions,
    read_json,
    update_history_from_data,
)
from scripts.update_asset_snapshot import ocr_image, parse_moze_account_ocr
from scripts.update_prices import fetch_fx_rate, fetch_prices


ROOT = Path(__file__).resolve().parent
TRANSACTIONS_PATH = DATA_DIR / "transactions.json"
DIVIDENDS_PATH = DATA_DIR / "dividends.json"
EXAMPLE_TRANSACTIONS_PATH = DATA_DIR / "example-transactions.json"
EXAMPLE_DIVIDENDS_PATH = DATA_DIR / "example-dividends.json"
EXAMPLE_PORTFOLIO_PATH = DATA_DIR / "example-portfolio.json"
EXAMPLE_HISTORY_PATH = DATA_DIR / "example-net-worth-history.json"
FINANCE_DATA_PATH = ROOT / "finance-data.js"
BACKUPS_DIR = DATA_DIR / "backups"
AUTH_USERNAME = os.getenv("ASSET_DASHBOARD_USERNAME", "admin").strip()
AUTH_PASSWORD = os.getenv("ASSET_DASHBOARD_PASSWORD", "").strip()
AUTH_REALM = "Personal Wealth Dashboard"
BACKUP_FILES = {
    "transactions.json": DATA_DIR / "transactions.json",
    "dividends.json": DATA_DIR / "dividends.json",
    "portfolio.json": DATA_DIR / "portfolio.json",
    "portfolio-data.js": DATA_DIR / "portfolio-data.js",
    "net-worth-history.json": DATA_DIR / "net-worth-history.json",
    "finance-data.js": ROOT / "finance-data.js",
    "accounts.json": DATA_DIR / "accounts.json",
}

app = FastAPI(title="Personal Wealth Dashboard Local API")


def utf8_json(payload: Any) -> Response:
    return Response(
        content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        media_type="application/json; charset=utf-8",
    )


def unauthorized_response() -> Response:
    return Response(
        status_code=401,
        content="Authentication required.",
        headers={"WWW-Authenticate": f'Basic realm="{AUTH_REALM}"'},
    )


def is_authorized(request: Request) -> bool:
    if not AUTH_PASSWORD:
        return False

    authorization = request.headers.get("authorization", "")
    scheme, _, credentials = authorization.partition(" ")
    if scheme.lower() != "basic" or not credentials:
        return False

    try:
        decoded = base64.b64decode(credentials, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return False

    username, separator, password = decoded.partition(":")
    if not separator:
        return False
    return secrets.compare_digest(username, AUTH_USERNAME) and secrets.compare_digest(password, AUTH_PASSWORD)


@app.middleware("http")
async def require_password(request: Request, call_next):
    if request.url.path in {"/api/health", "/api/auth-debug"}:
        return await call_next(request)
    if is_authorized(request):
        return await call_next(request)
    return unauthorized_response()


def read_demo_json(primary_path: Path, example_path: Path, default: Any) -> Any:
    if primary_path.exists():
        return read_json(primary_path, default)
    return read_json(example_path, default)


def read_transactions(use_examples: bool = False) -> list[dict[str, Any]]:
    transactions = db_store.read_transactions()
    if not transactions and use_examples:
        transactions = read_json(EXAMPLE_TRANSACTIONS_PATH, [])
    if not isinstance(transactions, list):
        raise HTTPException(status_code=500, detail="transactions.json 格式必須是陣列。")
    return transactions


def ensure_transaction_ids(transactions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    seen: set[str] = set()
    changed = False
    output: list[dict[str, Any]] = []
    for transaction in transactions:
        row = dict(transaction)
        transaction_id = str(row.get("id", "")).strip()
        if not transaction_id or transaction_id in seen:
            transaction_id = f"tx-{uuid4().hex[:12]}"
            row["id"] = transaction_id
            changed = True
        seen.add(transaction_id)
        output.append(row)
    return output, changed


def write_transactions(transactions: list[dict[str, Any]]) -> None:
    db_store.write_transactions(transactions)


def read_dividends(use_examples: bool = False) -> list[dict[str, Any]]:
    dividends = db_store.read_dividends()
    if not dividends and use_examples:
        dividends = read_json(EXAMPLE_DIVIDENDS_PATH, [])
    if not isinstance(dividends, list):
        raise HTTPException(status_code=500, detail="dividends.json 格式必須是陣列。")
    return dividends


def ensure_dividend_ids(dividends: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    seen: set[str] = set()
    changed = False
    output: list[dict[str, Any]] = []
    for dividend in dividends:
        row = dict(dividend)
        dividend_id = str(row.get("id", "")).strip()
        if not dividend_id or dividend_id in seen:
            dividend_id = f"div-{uuid4().hex[:12]}"
            row["id"] = dividend_id
            changed = True
        seen.add(dividend_id)
        output.append(row)
    return output, changed


def is_example_dividend(dividend: dict[str, Any]) -> bool:
    return (
        str(dividend.get("id", "")).strip() == "div-example"
        or "example only" in str(dividend.get("note", "")).lower()
        or "example" in str(dividend.get("name", "")).lower()
    )


def remove_example_dividends(dividends: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    cleaned = [dividend for dividend in dividends if not is_example_dividend(dividend)]
    return cleaned, len(cleaned) != len(dividends)


def write_dividends(dividends: list[dict[str, Any]]) -> None:
    db_store.write_dividends(dividends)


def parse_transaction_number(payload: dict[str, Any], key: str, label: str, allow_zero: bool = False) -> float:
    try:
        value = float(payload.get(key, 0))
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail=f"{label} 必須是數字。") from error
    if value < 0:
        raise HTTPException(status_code=400, detail=f"{label} 不可小於 0。")
    if value == 0 and not allow_zero:
        raise HTTPException(status_code=400, detail=f"{label} 必須大於 0。")
    return value


def parse_dividend_number(payload: dict[str, Any], key: str, label: str, allow_zero: bool = False) -> float:
    try:
        value = float(payload.get(key, 0))
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=400, detail=f"{label} 必須是數字。") from error
    if value < 0:
        raise HTTPException(status_code=400, detail=f"{label} 不可小於 0。")
    if value == 0 and not allow_zero:
        raise HTTPException(status_code=400, detail=f"{label} 必須大於 0。")
    return value


def normalize_transaction(payload: dict[str, Any]) -> dict[str, Any]:
    date = str(payload.get("date", "")).strip()
    market = str(payload.get("market", "")).strip().upper()
    symbol = str(payload.get("symbol", "")).strip().upper()
    name = str(payload.get("name", "")).strip()
    action = str(payload.get("action", "")).strip().upper()
    note = str(payload.get("note", payload.get("memo", ""))).strip()

    missing = [
        label
        for label, value in {
            "日期": date,
            "市場": market,
            "股票代號": symbol,
            "股票名稱": name,
            "動作": action,
        }.items()
        if not value
    ]
    if missing:
        raise HTTPException(status_code=400, detail=f"缺少欄位：{', '.join(missing)}。")
    if market not in {"TW", "US"}:
        raise HTTPException(status_code=400, detail="市場必須是 TW 或 US。")
    if action not in {"BUY", "SELL"}:
        raise HTTPException(status_code=400, detail="動作必須是 BUY 或 SELL。")

    return {
        **({"id": str(payload.get("id")).strip()} if payload.get("id") else {}),
        "date": date,
        "market": market,
        "symbol": symbol,
        "name": name,
        "action": action,
        "shares": parse_transaction_number(payload, "shares", "股數"),
        "price": parse_transaction_number(payload, "price", "成交價格"),
        "fee": parse_transaction_number(payload, "fee", "手續費", allow_zero=True),
        "note": note,
    }


def normalize_dividend(payload: dict[str, Any]) -> dict[str, Any]:
    date = str(payload.get("date", "")).strip()
    symbol = str(payload.get("symbol", "")).strip().upper()
    name = str(payload.get("name", "")).strip()
    market = str(payload.get("market", "")).strip().upper()
    currency = str(payload.get("currency", "")).strip().upper()
    note = str(payload.get("note", "")).strip()

    missing = [
        label
        for label, value in {
            "日期": date,
            "股票代號": symbol,
            "股票名稱": name,
            "市場": market,
            "幣別": currency,
        }.items()
        if not value
    ]
    if missing:
        raise HTTPException(status_code=400, detail=f"缺少欄位：{', '.join(missing)}。")
    if market not in {"TW", "US"}:
        raise HTTPException(status_code=400, detail="市場必須是 TW 或 US。")
    if currency not in {"TWD", "USD"}:
        raise HTTPException(status_code=400, detail="幣別必須是 TWD 或 USD。")

    return {
        **({"id": str(payload.get("id")).strip()} if payload.get("id") else {}),
        "date": date,
        "symbol": symbol,
        "name": name,
        "market": market,
        "amount": parse_dividend_number(payload, "amount", "股息金額"),
        "currency": currency,
        "tax": parse_dividend_number(payload, "tax", "扣稅", allow_zero=True),
        "note": note,
    }


def validate_positions(transactions: list[dict[str, Any]]) -> None:
    try:
        calculate_positions(transactions)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


def rebuild_portfolio_outputs() -> dict[str, Any]:
    portfolio = build_portfolio_from_data(
        db_store.read_transactions(),
        db_store.read_accounts({}),
        db_store.read_prices({"fxRate": 31.451, "prices": {}}),
        db_store.read_latest_portfolio({}),
    )
    history = update_history_from_data(portfolio, db_store.read_net_worth_history())
    db_store.write_portfolio_snapshot(portfolio)
    db_store.write_net_worth_history(history)
    return portfolio


def read_portfolio(use_examples: bool = False) -> dict[str, Any]:
    portfolio = db_store.read_latest_portfolio({})
    if not portfolio and db_store.has_private_data():
        portfolio = rebuild_portfolio_outputs()
    if not portfolio and use_examples:
        portfolio = read_json(EXAMPLE_PORTFOLIO_PATH, {})
    return portfolio


def read_net_worth_history(use_examples: bool = False) -> list[dict[str, Any]]:
    history = db_store.read_net_worth_history()
    if not history and use_examples:
        history = read_json(EXAMPLE_HISTORY_PATH, [])
    return history


def read_local_finance_data() -> dict[str, Any]:
    if not FINANCE_DATA_PATH.exists():
        return {}
    text = FINANCE_DATA_PATH.read_text(encoding="utf-8").strip()
    prefix = "window.financeData = "
    if not text.startswith(prefix):
        return {}
    payload = text[len(prefix):].strip()
    if payload.endswith(";"):
        payload = payload[:-1]
    data = json.loads(payload)
    return data if isinstance(data, dict) else {}


def is_full_backup_payload(payload: dict[str, Any]) -> bool:
    return any(key in payload for key in ["accounts", "transactions", "prices", "net-worth-history", "net_worth_history", "portfolio"])


def validate_full_backup_payload(payload: dict[str, Any]) -> None:
    required = {
        "accounts": isinstance(payload.get("accounts"), dict),
        "transactions": isinstance(payload.get("transactions"), list),
        "prices": isinstance(payload.get("prices"), dict),
        "net-worth-history": isinstance(payload.get("net-worth-history", payload.get("net_worth_history")), list),
        "portfolio": isinstance(payload.get("portfolio"), dict) and bool(payload.get("portfolio")),
    }
    missing = [label for label, ok in required.items() if not ok]
    if missing:
        raise HTTPException(status_code=400, detail=f"完整備份缺少或格式錯誤：{', '.join(missing)}。")


def backfill_history_from_finance_data(portfolio: dict[str, Any]) -> list[dict[str, Any]]:
    history = db_store.read_net_worth_history()
    finance_data = db_store.read_finance_data({})
    months = sorted(
        (
            month
            for year in finance_data.get("years", [])
            for month in year.get("months", [])
            if isinstance(month, dict) and isinstance(month.get("month"), str)
        ),
        key=lambda month: month["month"],
    )
    if not months:
        return history

    current_net_worth = round(float(portfolio.get("summary", {}).get("netWorth", 0)))
    by_date: dict[str, dict[str, Any]] = {
        str(row.get("date")): {"date": str(row.get("date")), "netWorth": round(float(row.get("netWorth", 0)))}
        for row in history
        if row.get("date")
    }
    running = current_net_worth
    for month in reversed(months):
        month_key = month["month"]
        date_key = f"{month_key}-01"
        by_date.setdefault(date_key, {"date": date_key, "netWorth": running})
        running -= round(float(month.get("net", 0) or 0))
    output = sorted(by_date.values(), key=lambda row: row["date"])
    db_store.write_net_worth_history(output)
    return output


def transaction_response(transactions: list[dict[str, Any]], transaction: dict[str, Any] | None, portfolio: dict[str, Any]) -> dict:
    return {
        "ok": True,
        "transaction": transaction,
        "transactions": transactions,
        "portfolioSummary": portfolio["summary"],
    }


def backup_folder_name() -> str:
    return datetime.now(TWD).strftime("%Y-%m-%d_%H-%M-%S")


def backup_path(name: str) -> Path:
    if "/" in name or "\\" in name or name in {"", ".", ".."}:
        raise HTTPException(status_code=400, detail="備份資料夾名稱不合法。")
    path = BACKUPS_DIR / name
    if not path.exists() or not path.is_dir():
        raise HTTPException(status_code=404, detail="找不到備份資料夾。")
    return path


def backup_info(path: Path) -> dict[str, Any]:
    files = sorted(file.name for file in path.iterdir() if file.is_file())
    return {
        "name": path.name,
        "files": files,
        "fileCount": len(files),
    }


def parse_manual_amount(value: Optional[str], label: str) -> Optional[int]:
    if value is None or value.strip() == "":
        return None
    cleaned = value.replace(",", "").replace("NT$", "").replace("$", "").strip()
    try:
        return round(float(cleaned))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=f"{label} 必須是數字。") from error


async def save_upload(upload: UploadFile, folder: Path) -> Path:
    safe_name = Path(upload.filename or "upload").name
    suffix = Path(safe_name).suffix
    target = folder / safe_name
    if not suffix:
        target = folder / "upload"
    target.write_bytes(await upload.read())
    return target


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/auth-debug")
def auth_debug() -> dict[str, Any]:
    return {
        "usernameConfigured": bool(AUTH_USERNAME),
        "passwordConfigured": bool(AUTH_PASSWORD),
        "usernameLength": len(AUTH_USERNAME or ""),
        "passwordLength": len(AUTH_PASSWORD or ""),
        "usernameValue": AUTH_USERNAME,
    }


@app.get("/api/db/status")
def db_status() -> Response:
    return utf8_json(db_store.status())


@app.get("/api/db/debug-supabase")
def debug_supabase() -> dict[str, Any]:
    return db_store.debug_supabase()


@app.get("/api/portfolio")
def get_portfolio() -> dict[str, Any]:
    portfolio = read_portfolio(use_examples=True)
    return {"ok": True, "portfolio": portfolio, "source": db_store.active_backend() if portfolio else "example"}


@app.get("/api/accounts")
def get_accounts() -> dict[str, Any]:
    accounts = db_store.read_accounts({})
    return {"ok": True, "accounts": accounts, "source": db_store.active_backend() if db_store.table_count("accounts") else "empty"}


@app.get("/api/prices")
def get_prices() -> dict[str, Any]:
    prices = db_store.read_prices({"fxRate": 31.451, "prices": {}})
    return {"ok": True, "prices": prices, "source": db_store.active_backend() if prices.get("prices") else "empty"}


@app.get("/api/net-worth-history")
def get_net_worth_history() -> dict[str, Any]:
    history = read_net_worth_history(use_examples=True)
    return {"ok": True, "history": history, "source": db_store.active_backend() if history else "example"}


@app.get("/api/finance-data")
def get_finance_data() -> dict[str, Any]:
    finance_data = db_store.read_finance_data({})
    return {
        "ok": True,
        "financeData": finance_data,
        "source": db_store.active_backend() if finance_data else "empty",
    }


@app.post("/api/finance-data")
async def import_finance_data(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as error:
        raise HTTPException(status_code=400, detail="對帳資料 JSON 格式錯誤。") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="對帳資料 JSON 必須是物件格式。")

    finance_data = payload.get("financeData", payload)
    if not isinstance(finance_data, dict) or not isinstance(finance_data.get("years"), list):
        raise HTTPException(status_code=400, detail="對帳資料必須包含 years 陣列。")

    saved_to = db_store.write_finance_data(finance_data)
    db_store.set_metadata("lastFinanceDataImport", db_store.now_iso())
    portfolio = read_portfolio(use_examples=False)
    history = backfill_history_from_finance_data(portfolio) if portfolio else db_store.read_net_worth_history()
    return {
        "ok": True,
        "currentDb": db_store.active_backend(),
        "savedTo": saved_to,
        "years": len(finance_data.get("years", [])),
        "recordCount": finance_data.get("recordCount", 0),
        "historyCount": len(history),
    }


@app.post("/api/db/import-json")
async def import_json_to_db(backup: Optional[UploadFile] = File(None)) -> dict[str, Any]:
    if backup is not None and backup.filename:
        try:
            payload = json.loads((await backup.read()).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=400, detail="備份 JSON 格式錯誤。") from error
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="備份 JSON 必須是物件格式。")
        if is_full_backup_payload(payload):
            validate_full_backup_payload(payload)

        imported: list[str] = []
        accounts = payload.get("accounts")
        clean_payload: dict[str, Any] = {}
        if isinstance(accounts, dict):
            clean_payload["accounts"] = accounts
            imported.append("accounts")

        transactions = payload.get("transactions")
        if isinstance(transactions, list):
            transactions, _ = ensure_transaction_ids(transactions)
            clean_payload["transactions"] = transactions
            imported.append("transactions")

        dividends = payload.get("dividends")
        if isinstance(dividends, list):
            dividends, _ = ensure_dividend_ids(dividends)
            clean_payload["dividends"] = dividends
            imported.append("dividends")

        prices = payload.get("prices")
        if isinstance(prices, dict):
            clean_payload["prices"] = prices
            imported.append("prices")

        history = payload.get("net-worth-history", payload.get("net_worth_history"))
        if isinstance(history, list):
            clean_payload["net-worth-history"] = history
            imported.append("net-worth-history")

        finance_data = payload.get("financeData", payload.get("finance-data"))
        if isinstance(finance_data, dict):
            clean_payload["financeData"] = finance_data
            imported.append("financeData")

        if imported == ["financeData"]:
            saved_to = db_store.write_finance_data(finance_data)
            db_store.set_metadata("lastFinanceDataImport", db_store.now_iso())
            portfolio = read_portfolio(use_examples=False)
            history = backfill_history_from_finance_data(portfolio) if portfolio else db_store.read_net_worth_history()
            return {
                "ok": True,
                "imported": imported,
                "counts": db_store.status()["counts"],
                "currentDb": db_store.active_backend(),
                "savedTo": saved_to,
                "historyCount": len(history),
                "message": "年度/月度對帳資料已匯入，不影響投資與資產資料。",
            }

        safety_backup = db_store.export_backup()
        try:
            db_store.import_backup_payload(clean_payload)
        except Exception:
            db_store.import_backup_payload(safety_backup)
            raise

        portfolio = rebuild_portfolio_outputs()
        db_store.set_metadata("lastImport", db_store.now_iso())
        return {
            "ok": True,
            "imported": imported,
            "counts": db_store.status()["counts"],
            "portfolioSummary": portfolio.get("summary", {}),
            "currentDb": db_store.active_backend(),
            "message": "備份 JSON 已匯入資料庫並重建投資組合。",
        }

    imported: list[str] = []

    accounts = read_json(DATA_DIR / "accounts.json", None)
    if isinstance(accounts, dict):
        db_store.write_accounts(accounts)
        imported.append("accounts.json")

    transactions = read_json(DATA_DIR / "transactions.json", None)
    if isinstance(transactions, list):
        transactions, _ = ensure_transaction_ids(transactions)
        db_store.write_transactions(transactions)
        imported.append("transactions.json")

    dividends = read_json(DATA_DIR / "dividends.json", None)
    if isinstance(dividends, list):
        dividends, _ = ensure_dividend_ids(dividends)
        db_store.write_dividends(dividends)
        imported.append("dividends.json")

    prices = read_json(DATA_DIR / "prices.json", None)
    if isinstance(prices, dict):
        db_store.write_prices(prices)
        imported.append("prices.json")

    history = read_json(DATA_DIR / "net-worth-history.json", None)
    if isinstance(history, list):
        db_store.write_net_worth_history(history)
        imported.append("net-worth-history.json")

    finance_data = read_local_finance_data()
    if finance_data:
        db_store.write_finance_data(finance_data)
        imported.append("finance-data.js")

    portfolio = rebuild_portfolio_outputs()
    db_store.set_metadata("lastImport", db_store.now_iso())
    return {
        "ok": True,
        "imported": imported,
        "counts": db_store.status()["counts"],
        "portfolioSummary": portfolio.get("summary", {}),
        "currentDb": db_store.active_backend(),
        "message": "JSON 匯入資料庫完成。正式 JSON 仍應留在本機並由 .gitignore 忽略。",
    }


@app.get("/api/db/export-json")
def export_json_backup() -> JSONResponse:
    backup = db_store.export_backup()
    db_store.set_metadata("lastBackup", backup["exportedAt"])
    response = JSONResponse(content=backup)
    response.headers["Content-Disposition"] = f'attachment; filename="wealth-dashboard-backup-{datetime.now(TWD).strftime("%Y%m%d-%H%M%S")}.json"'
    return response


@app.post("/api/db/backfill-net-worth-history")
def backfill_net_worth_history() -> dict[str, Any]:
    portfolio = read_portfolio(use_examples=False)
    if not portfolio:
        raise HTTPException(status_code=400, detail="尚無正式 portfolio，無法補齊淨資產歷史。")
    history = backfill_history_from_finance_data(portfolio)
    return {
        "ok": True,
        "currentDb": db_store.active_backend(),
        "historyCount": len(history),
        "firstDate": history[0]["date"] if history else "",
        "lastDate": history[-1]["date"] if history else "",
    }


@app.post("/api/db/rebuild-portfolio")
def rebuild_portfolio_from_db() -> dict[str, Any]:
    portfolio = rebuild_portfolio_outputs()
    return {
        "ok": True,
        "currentDb": db_store.active_backend(),
        "counts": db_store.status()["counts"],
        "portfolioSummary": portfolio.get("summary", {}),
    }


@app.post("/api/db/migrate-to-supabase")
def migrate_to_supabase() -> dict[str, Any]:
    try:
        result = db_store.migrate_sqlite_to_supabase()
    except Exception as error:
        raise HTTPException(status_code=400, detail=f"Supabase 遷移失敗：{error}") from error
    db_store.set_metadata("lastMigration", db_store.now_iso())
    return result


@app.get("/api/backups")
def list_backups() -> dict:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    backups = [backup_info(path) for path in BACKUPS_DIR.iterdir() if path.is_dir()]
    backups.sort(key=lambda row: row["name"], reverse=True)
    return {"ok": True, "backups": backups}


@app.post("/api/backups")
def create_backup() -> dict:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    target = BACKUPS_DIR / backup_folder_name()
    counter = 1
    while target.exists():
        target = BACKUPS_DIR / f"{backup_folder_name()}-{counter}"
        counter += 1
    target.mkdir(parents=True)

    copied: list[str] = []
    missing: list[str] = []
    for filename, source in BACKUP_FILES.items():
        if not source.exists():
            missing.append(filename)
            continue
        shutil.copy2(source, target / filename)
        copied.append(filename)

    if missing:
        shutil.rmtree(target, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"備份失敗，缺少檔案：{', '.join(missing)}。")

    return {"ok": True, "backup": backup_info(target), "message": "備份完成。"}


@app.post("/api/backups/{backup_name}/restore")
def restore_backup(backup_name: str) -> dict:
    source_dir = backup_path(backup_name)
    missing = [
        filename
        for filename in BACKUP_FILES
        if filename != "dividends.json" and not (source_dir / filename).exists()
    ]
    if missing:
        raise HTTPException(status_code=400, detail=f"備份不完整，缺少檔案：{', '.join(missing)}。")

    for filename, target in BACKUP_FILES.items():
        if not (source_dir / filename).exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_dir / filename, target)

    return {"ok": True, "backup": backup_info(source_dir), "message": "還原完成。"}


@app.get("/api/transactions")
def get_transactions() -> dict:
    transactions = read_transactions(use_examples=True)
    transactions, changed = ensure_transaction_ids(transactions)
    if changed:
        write_transactions(transactions)
    return {"ok": True, "transactions": transactions, "source": db_store.active_backend() if transactions else "example"}


@app.post("/api/transactions")
async def create_transaction(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception as error:
        raise HTTPException(status_code=400, detail="交易資料格式錯誤。") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="交易資料格式錯誤。")

    transaction = normalize_transaction(payload)
    transaction["id"] = f"tx-{uuid4().hex[:12]}"
    transactions, changed = ensure_transaction_ids(read_transactions())
    if changed:
        write_transactions(transactions)
    next_transactions = transactions + [transaction]
    validate_positions(next_transactions)

    write_transactions(next_transactions)
    portfolio = rebuild_portfolio_outputs()
    return transaction_response(next_transactions, transaction, portfolio)


@app.put("/api/transactions/{transaction_id}")
async def update_transaction(transaction_id: str, request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception as error:
        raise HTTPException(status_code=400, detail="交易資料格式錯誤。") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="交易資料格式錯誤。")

    transactions, changed = ensure_transaction_ids(read_transactions())
    index = next((idx for idx, row in enumerate(transactions) if row.get("id") == transaction_id), None)
    if index is None:
        raise HTTPException(status_code=404, detail="找不到交易紀錄。")

    transaction = normalize_transaction({**payload, "id": transaction_id})
    next_transactions = transactions[:]
    next_transactions[index] = transaction
    validate_positions(next_transactions)

    write_transactions(next_transactions)
    portfolio = rebuild_portfolio_outputs()
    return transaction_response(next_transactions, transaction, portfolio)


@app.delete("/api/transactions/{transaction_id}")
def delete_transaction(transaction_id: str) -> dict:
    transactions, changed = ensure_transaction_ids(read_transactions())
    next_transactions = [row for row in transactions if row.get("id") != transaction_id]
    if len(next_transactions) == len(transactions):
        raise HTTPException(status_code=404, detail="找不到交易紀錄。")

    validate_positions(next_transactions)
    write_transactions(next_transactions)
    portfolio = rebuild_portfolio_outputs()
    return transaction_response(next_transactions, None, portfolio)


@app.get("/api/dividends")
def get_dividends() -> dict:
    dividends = read_dividends(use_examples=False)
    dividends, removed_examples = remove_example_dividends(dividends)
    dividends, changed = ensure_dividend_ids(dividends)
    if removed_examples or changed:
        write_dividends(dividends)
    return {"ok": True, "dividends": dividends, "source": db_store.active_backend() if dividends else "empty"}


@app.post("/api/dividends")
async def create_dividend(request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception as error:
        raise HTTPException(status_code=400, detail="股息資料格式錯誤。") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="股息資料格式錯誤。")

    dividend = normalize_dividend(payload)
    dividend["id"] = f"div-{uuid4().hex[:12]}"
    dividends, changed = ensure_dividend_ids(read_dividends())
    if changed:
        write_dividends(dividends)
    next_dividends = dividends + [dividend]
    write_dividends(next_dividends)
    return {"ok": True, "dividend": dividend, "dividends": next_dividends}


@app.put("/api/dividends/{dividend_id}")
async def update_dividend(dividend_id: str, request: Request) -> dict:
    try:
        payload = await request.json()
    except Exception as error:
        raise HTTPException(status_code=400, detail="股息資料格式錯誤。") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="股息資料格式錯誤。")

    dividends, changed = ensure_dividend_ids(read_dividends())
    index = next((idx for idx, row in enumerate(dividends) if row.get("id") == dividend_id), None)
    if index is None:
        raise HTTPException(status_code=404, detail="找不到股息紀錄。")

    dividend = normalize_dividend({**payload, "id": dividend_id})
    next_dividends = dividends[:]
    next_dividends[index] = dividend
    write_dividends(next_dividends)
    return {"ok": True, "dividend": dividend, "dividends": next_dividends}


@app.delete("/api/dividends/{dividend_id}")
def delete_dividend(dividend_id: str) -> dict:
    dividends, changed = ensure_dividend_ids(read_dividends())
    next_dividends = [row for row in dividends if row.get("id") != dividend_id]
    if len(next_dividends) == len(dividends):
        raise HTTPException(status_code=404, detail="找不到股息紀錄。")

    write_dividends(next_dividends)
    return {"ok": True, "dividend": None, "dividends": next_dividends}


@app.post("/api/update-asset-snapshot")
async def update_asset_snapshot(
    request: Request,
    csv: Optional[UploadFile] = File(None),
    image: Optional[UploadFile] = File(None),
    cash: Optional[str] = Form(None),
    bank: Optional[str] = Form(None),
    creditCardDebt: Optional[str] = Form(None),
) -> dict:
    form = await request.form()

    def form_text(*names: str) -> Optional[str]:
        for name in names:
            value = form.get(name)
            if value is None or hasattr(value, "filename"):
                continue
            text = str(value).strip()
            if text:
                return text
        return None

    cash = cash or form_text("cash")
    bank = bank or form_text("bank")
    creditCardDebt = creditCardDebt or form_text("creditCardDebt", "debt", "credit_card_debt")

    manual_cash = parse_manual_amount(cash, "現金")
    manual_bank = parse_manual_amount(bank, "銀行")
    manual_debt = parse_manual_amount(creditCardDebt, "信用卡負債")
    manual_values = {
        "現金": manual_cash,
        "銀行": manual_bank,
        "信用卡負債": manual_debt,
    }
    has_any_manual = any(value is not None for value in manual_values.values())
    has_all_manual = all(value is not None for value in manual_values.values())

    if has_any_manual and not has_all_manual:
        missing = [label for label, value in manual_values.items() if value is None]
        raise HTTPException(status_code=400, detail=f"手動輸入時缺少：{', '.join(missing)}。")

    with tempfile.TemporaryDirectory(prefix="wealth-snapshot-") as tmp:
        tmp_path = Path(tmp)
        imported_csv = False
        if csv is None:
            csv_upload = form.get("csv")
            if hasattr(csv_upload, "filename"):
                csv = csv_upload
        if image is None:
            image_upload = form.get("image")
            if hasattr(image_upload, "filename"):
                image = image_upload

        if csv is not None and csv.filename:
            csv_path = await save_upload(csv, tmp_path)
            try:
                import_moze_csv(csv_path)
            except Exception as error:
                raise HTTPException(status_code=400, detail=f"MOZE CSV 匯入失敗：{error}") from error
            imported_csv = True

        if has_all_manual:
            amounts = {
                "cash": manual_cash or 0,
                "bank": manual_bank or 0,
                "debt": manual_debt or 0,
                "source": "manual",
            }
        elif image is not None and image.filename:
            image_path = await save_upload(image, tmp_path)
            try:
                text = ocr_image(image_path)
                (DATA_DIR / "last-moze-ocr.txt").write_text(text, encoding="utf-8")
                parsed = parse_moze_account_ocr(text)
            except Exception as error:
                raise HTTPException(
                    status_code=400,
                    detail=f"OCR 無法可靠辨識，請改用手動輸入。{error}",
                ) from error
            amounts = {**parsed, "source": "ocr"}
        elif imported_csv:
            accounts = db_store.read_accounts({})
            cash_twd = round(float(accounts.get("cashTWD", 0)))
            amounts = {
                "cash": cash_twd,
                "bank": 0,
                "debt": round(float(accounts.get("creditCardDebt", 0))),
                "source": "existing_accounts",
            }
        else:
            raise HTTPException(
                status_code=400,
                detail="請至少上傳 MOZE CSV、帳戶總覽截圖，或填寫現金、銀行、信用卡負債。",
            )

    if amounts["source"] == "existing_accounts":
        accounts = db_store.read_accounts({})
    else:
        accounts = db_store.read_accounts({})
        accounts["cashTWD"] = amounts["cash"] + amounts["bank"]
        accounts["creditCardDebt"] = amounts["debt"]
        accounts.setdefault("cashUSD", 0)
        accounts.setdefault("otherDebt", 0)
        db_store.write_accounts(accounts)
    portfolio = rebuild_portfolio_outputs()

    return {
        "ok": True,
        "source": amounts["source"],
        "cash": amounts["cash"],
        "bank": amounts["bank"],
        "creditCardDebt": amounts["debt"],
        "cashTWD": accounts["cashTWD"],
        "totalAssets": portfolio["summary"]["totalAssets"],
        "netWorth": portfolio["summary"]["netWorth"],
    }


@app.post("/api/update-prices")
def update_prices(request: Request) -> dict:
    if request.headers.get("x-price-check") == "1":
        return {"ok": True, "method": "POST", "message": "股價更新 API 已就緒。"}

    try:
        has_formal_data = bool(
            db_store.read_transactions()
            or db_store.read_accounts({})
            or db_store.read_latest_portfolio({})
        )
        if not has_formal_data:
            portfolio = read_portfolio(use_examples=True)
            warnings = ["目前是範例資料，請先匯入正式備份"]
            current_db = db_store.active_backend()
            return {
                "ok": True,
                "currentDb": current_db,
                "savedTo": "none",
                "updatedSymbols": [],
                "failedSymbols": [],
                "errorMessages": warnings,
                "twResult": {"updatedSymbols": [], "failedSymbols": [], "symbols": {}},
                "usResult": {"updatedSymbols": [], "failedSymbols": [], "symbols": {}},
                "source": "demo",
                "updatedAt": portfolio.get("updatedAt", ""),
                "fxRate": portfolio.get("fxRate"),
                "warnings": warnings,
                "marketUpdates": {
                    "TW": portfolio.get("markets", {}).get("TW", {}).get("updatedAt", ""),
                    "US": portfolio.get("markets", {}).get("US", {}).get("updatedAt", ""),
                },
                "portfolioSummary": portfolio.get("summary", {}),
                "updatedHoldings": 0,
                "totalHoldings": len(portfolio.get("holdings", [])),
            }

        current_portfolio = read_portfolio(use_examples=False)
        holdings = current_portfolio.get("holdings", [])
        fx_rate = fetch_fx_rate(float(current_portfolio.get("fxRate") or 31.451))
        latest_prices, warnings, price_details = fetch_prices(holdings)
        stored_prices = db_store.read_prices({"fxRate": fx_rate, "prices": {}})
        stored_prices["fxRate"] = fx_rate
        stored_prices["prices"] = {**stored_prices.get("prices", {}), **latest_prices}
        saved_to = db_store.write_prices(stored_prices)
        portfolio = rebuild_portfolio_outputs()
        current_db = db_store.active_backend()
        if not portfolio:
            portfolio = current_portfolio
        db_store.set_metadata("lastPriceUpdate", db_store.now_iso())
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"股價更新失敗：{error}") from error
    return {
        "ok": True,
        "currentDb": current_db,
        "savedTo": saved_to,
        "updatedSymbols": price_details["updatedSymbols"],
        "failedSymbols": price_details["failedSymbols"],
        "errorMessages": price_details["errorMessages"],
        "twResult": price_details["twResult"],
        "usResult": price_details["usResult"],
        "source": current_db,
        "updatedAt": portfolio.get("updatedAt", ""),
        "fxRate": portfolio.get("fxRate"),
        "warnings": warnings,
        "marketUpdates": {
            "TW": portfolio.get("markets", {}).get("TW", {}).get("updatedAt", ""),
            "US": portfolio.get("markets", {}).get("US", {}).get("updatedAt", ""),
        },
        "portfolioSummary": portfolio.get("summary", {}),
        "updatedHoldings": len(latest_prices),
        "totalHoldings": len(holdings),
    }


@app.get("/api/update-prices")
def update_prices_status() -> dict:
    return {"ok": True, "method": "POST", "message": "股價更新 API 已就緒。"}


@app.api_route("/settings.html", methods=["GET", "HEAD"])
def settings_page() -> FileResponse:
    return FileResponse(ROOT / "settings.html")


@app.api_route("/settings", methods=["GET", "HEAD"])
def settings_redirect() -> RedirectResponse:
    return RedirectResponse(url="/settings.html", status_code=307)


app.mount("/", StaticFiles(directory=ROOT, html=True), name="static")
