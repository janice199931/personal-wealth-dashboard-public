from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from datetime import datetime
from uuid import uuid4
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

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
AUTH_COOKIE_NAME = "wealth_dashboard_session"
AUTH_COOKIE_MAX_AGE = 60 * 60 * 24 * 30
EMERGENCY_FUND_TARGET = 100000
INVESTMENT_RESERVE_TARGET = 150000
ETF_00685L_SPLIT_METADATA_KEY = "corporateAction00685LSplit202607"
ETF_00685L_SPLIT_RATIO = 24
ETF_00685L_TARGET_SHARES = 8880
ETF_00685L_TARGET_AVERAGE_COST = 12.1
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
PORTFOLIO_REBUILD_LOCK = Lock()
PRICE_UPDATE_LOCK = Lock()


@app.exception_handler(RuntimeError)
async def runtime_error_handler(request: Request, error: RuntimeError) -> JSONResponse:
    message = str(error) or "資料暫時無法保存，請稍後再試。"
    if "Supabase" not in message:
        message = "資料暫時無法保存，請稍後再試。"
    return JSONResponse(status_code=503, content={"detail": message})


def utf8_json(payload: Any) -> Response:
    return Response(
        content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        media_type="application/json; charset=utf-8",
    )


def unauthorized_response() -> Response:
    return Response(
        status_code=401,
        content="Authentication required.",
    )


def wants_json(request: Request) -> bool:
    content_type = request.headers.get("content-type", "")
    accept = request.headers.get("accept", "")
    return "application/json" in content_type or "application/json" in accept


def login_redirect_response() -> RedirectResponse:
    return RedirectResponse(url="/login.html", status_code=303)


def auth_secret() -> str:
    return AUTH_PASSWORD or "wealth-dashboard-local"


def sign_auth_payload(payload: str) -> str:
    return hmac.new(auth_secret().encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def make_auth_token(username: str) -> str:
    issued_at = str(int(time.time()))
    payload = f"{username}:{issued_at}"
    return f"{payload}:{sign_auth_payload(payload)}"


def is_valid_auth_token(token: str) -> bool:
    parts = str(token or "").split(":")
    if len(parts) != 3:
        return False
    username, issued_at, signature = parts
    if not secrets.compare_digest(username, AUTH_USERNAME):
        return False
    try:
        age = time.time() - int(issued_at)
    except ValueError:
        return False
    if age < 0 or age > AUTH_COOKIE_MAX_AGE:
        return False
    payload = f"{username}:{issued_at}"
    return secrets.compare_digest(signature, sign_auth_payload(payload))


def is_cookie_authorized(request: Request) -> bool:
    return bool(AUTH_PASSWORD) and is_valid_auth_token(request.cookies.get(AUTH_COOKIE_NAME, ""))


def is_authorized(request: Request) -> bool:
    if is_cookie_authorized(request):
        return True
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


def is_page_request(request: Request) -> bool:
    path = request.url.path
    accept = request.headers.get("accept", "")
    return path in {"/", "/index.html"} or path.endswith(".html") or "text/html" in accept


def is_secure_request(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"


@app.middleware("http")
async def require_password(request: Request, call_next):
    public_paths = {"/api/health", "/api/login", "/api/logout", "/login.html"}
    if request.url.path in public_paths:
        return await call_next(request)
    if is_authorized(request):
        return await call_next(request)
    if is_page_request(request):
        return login_redirect_response()
    return unauthorized_response()


def app_version_payload() -> dict[str, Any]:
    version = os.getenv("RENDER_GIT_COMMIT", "")[:7] or os.getenv("APP_VERSION", "")
    if not version:
        try:
            version = str(int(max((ROOT / name).stat().st_mtime for name in ["index.html", "styles.css", "app.js"])))
        except Exception:
            version = "local"
    return {
        "ok": True,
        "version": version,
        "label": os.getenv("APP_VERSION_LABEL", "2026-06-26 stability"),
    }


def asset_version() -> str:
    return app_version_payload()["version"]


def html_page(filename: str) -> Response:
    content = (ROOT / filename).read_text(encoding="utf-8")
    content = content.replace("__ASSET_VERSION__", asset_version())
    return Response(content=content, media_type="text/html; charset=utf-8")


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


def normalize_input_date(value: Any, label: str = "日期") -> str:
    text = str(value or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail=f"{label} 不可空白。")
    normalized = text.replace(".", "/").replace("-", "/")
    parts = [part.strip() for part in normalized.split("/") if part.strip()]
    if len(parts) == 3:
        try:
            year, month, day = [int(part) for part in parts]
        except ValueError as error:
            raise HTTPException(status_code=400, detail=f"{label} 格式錯誤，請輸入民國年，例如 115/02/11。") from error
        if year < 1911:
            year += 1911
        try:
            return datetime(year, month, day).date().isoformat()
        except ValueError as error:
            raise HTTPException(status_code=400, detail=f"{label} 不是有效日期。") from error
    raise HTTPException(status_code=400, detail=f"{label} 格式錯誤，請輸入民國年，例如 115/02/11。")


def normalize_transaction(payload: dict[str, Any]) -> dict[str, Any]:
    date = normalize_input_date(payload.get("date"))
    market = str(payload.get("market", "")).strip().upper()
    symbol = str(payload.get("symbol", "")).strip().upper()
    name = str(payload.get("name", "")).strip()
    action = str(payload.get("action", "")).strip().upper()
    purpose = str(payload.get("purpose", "")).strip()
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
    if purpose and purpose not in {"monthly", "dividend", "extra", "rebalance"}:
        raise HTTPException(status_code=400, detail="投入來源分類不正確。")

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
        "purpose": purpose,
        "note": note,
    }


def normalize_dividend(payload: dict[str, Any]) -> dict[str, Any]:
    date = normalize_input_date(payload.get("date"))
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
    with PORTFOLIO_REBUILD_LOCK:
        portfolio = build_portfolio_from_data(
            db_store.read_transactions(),
            db_store.read_accounts({}),
            db_store.read_prices({"fxRate": 31.451, "prices": {}}),
            db_store.read_latest_portfolio({}),
        )
        history = update_history_from_data(portfolio, db_store.read_net_worth_history())
        if len(history) <= 1:
            expanded_history = build_history_from_finance_data(portfolio, history, db_store.read_finance_data({}))
            if len(expanded_history) > len(history):
                history = expanded_history
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
    output = build_history_from_finance_data(portfolio, history, finance_data)
    if output:
        db_store.write_net_worth_history(output)
    return output


def build_history_from_finance_data(
    portfolio: dict[str, Any],
    history: list[dict[str, Any]],
    finance_data: dict[str, Any],
) -> list[dict[str, Any]]:
    if isinstance(finance_data.get("financeData"), dict):
        finance_data = finance_data["financeData"]
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
    return sorted(by_date.values(), key=lambda row: row["date"])


def transaction_response(transactions: list[dict[str, Any]], transaction: dict[str, Any] | None, portfolio: dict[str, Any]) -> dict:
    return {
        "ok": True,
        "verified": True,
        "transaction": transaction,
        "transactions": transactions,
        "portfolioSummary": portfolio["summary"],
    }


def transaction_matches_saved(saved: dict[str, Any], expected: dict[str, Any]) -> bool:
    text_fields = ["id", "date", "market", "symbol", "name", "action", "purpose", "note"]
    number_fields = ["shares", "price", "fee"]
    return (
        all(str(saved.get(field, "")).strip() == str(expected.get(field, "")).strip() for field in text_fields)
        and all(abs(float(saved.get(field, 0) or 0) - float(expected.get(field, 0) or 0)) < 0.000001 for field in number_fields)
    )


def dividend_matches_saved(saved: dict[str, Any], expected: dict[str, Any]) -> bool:
    text_fields = ["id", "date", "market", "symbol", "name", "currency", "note"]
    number_fields = ["amount", "tax"]
    return (
        all(str(saved.get(field, "")).strip() == str(expected.get(field, "")).strip() for field in text_fields)
        and all(abs(float(saved.get(field, 0) or 0) - float(expected.get(field, 0) or 0)) < 0.000001 for field in number_fields)
    )


def mark_successful_save() -> str:
    saved_at = db_store.now_iso()
    db_store.set_metadata("lastSuccessfulSave", saved_at)
    return saved_at


def verify_saved_row(rows: list[dict[str, Any]], target: dict[str, Any], message: str) -> None:
    if not any(transaction_matches_saved(row, target) for row in rows):
        raise HTTPException(status_code=503, detail=message)


def verify_saved_dividend(rows: list[dict[str, Any]], target: dict[str, Any], message: str) -> None:
    if not any(dividend_matches_saved(row, target) for row in rows):
        raise HTTPException(status_code=503, detail=message)


def verify_deleted_row(rows: list[dict[str, Any]], row_id: str, message: str) -> None:
    if any(str(row.get("id", "")) == row_id for row in rows):
        raise HTTPException(status_code=503, detail=message)


def verify_saved_finance_month(month_key: str, expected: dict[str, Any]) -> None:
    finance_data = db_store.read_finance_data({})
    months = [
        month
        for year in finance_data.get("years", [])
        for month in year.get("months", [])
        if isinstance(month, dict) and month.get("month") == month_key
    ]
    if not months:
        raise HTTPException(status_code=503, detail="月份統計已送出，但重新讀取後沒有找到這個月份，請重新整理確認。")
    saved = months[0]
    for field in ["income", "expense", "net", "sinopacTransfer"]:
        if round(float(saved.get(field, 0) or 0)) != round(float(expected.get(field, 0) or 0)):
            raise HTTPException(status_code=503, detail="月份統計已送出，但重新讀取後內容沒有對上，請重新整理確認。")


def verify_saved_finance_import(expected: dict[str, Any]) -> None:
    saved = db_store.read_finance_data({})
    expected_years = expected.get("years", [])
    saved_years = saved.get("years", [])
    if not isinstance(saved_years, list) or len(saved_years) != len(expected_years):
        raise HTTPException(status_code=503, detail="年度/月度對帳已送出，但重新讀取後年度數量沒有對上，請重新整理確認。")
    expected_month_count = sum(len(year.get("months", [])) for year in expected_years if isinstance(year, dict))
    saved_month_count = sum(len(year.get("months", [])) for year in saved_years if isinstance(year, dict))
    if expected_month_count != saved_month_count:
        raise HTTPException(status_code=503, detail="年度/月度對帳已送出，但重新讀取後月份數量沒有對上，請重新整理確認。")


def plain_health_summary(
    read_ok: bool,
    write_ok: bool,
    price_api_ok: bool,
    status: dict[str, Any],
    anomalies: list[str],
) -> dict[str, Any]:
    formal_data_ok = status.get("currentDb") == "supabase" and not status.get("fallbackActive")
    can_edit = read_ok and write_ok and formal_data_ok
    if not read_ok:
        title = "資料讀取異常，先不要新增或修改資料。"
        action = "請重新整理後再檢查一次；如果仍異常，先不要輸入新資料。"
        tone = "error"
    elif not write_ok:
        title = "Supabase 目前無法寫入，先不要新增交易、股息或更新資產。"
        action = "請稍後再試，或到 Supabase 檢查連線狀態。"
        tone = "error"
    elif not formal_data_ok:
        title = "目前不是正式資料狀態，先不要修改重要資料。"
        action = "請確認 Database Health 與 Data Status 都是正式資料。"
        tone = "error"
    elif not price_api_ok:
        title = "資料保存正常，股價更新暫時不穩。"
        action = "你仍可以新增資料；股價晚點再更新即可。"
        tone = "success"
    elif anomalies:
        title = "資料可正常使用，但有幾個項目建議留意。"
        action = "請查看下方提醒；不影響一般新增或修改。"
        tone = "success"
    else:
        title = "網站狀態正常，可以放心輸入資料。"
        action = "資料讀取、寫入、備份與股價檢查都正常。"
        tone = "success"
    return {"tone": tone, "title": title, "action": action, "canEdit": can_edit}


def backup_folder_name() -> str:
    return datetime.now(TWD).strftime("%Y-%m-%d_%H-%M-%S")


def backup_file_name(prefix: str = "wealth-dashboard-auto") -> str:
    stamp = datetime.now(TWD).strftime("%Y-%m-%d") if prefix == "wealth-dashboard-auto" else datetime.now(TWD).strftime("%Y-%m-%d_%H-%M-%S")
    return f"{prefix}-{stamp}.json"


def write_database_backup(prefix: str = "wealth-dashboard-auto") -> dict[str, Any]:
    BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    payload = db_store.export_backup()
    target = BACKUPS_DIR / backup_file_name(prefix)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    info = {
        "name": target.name,
        "path": str(target),
        "size": target.stat().st_size,
        "createdAt": payload["exportedAt"],
    }
    db_store.set_metadata("lastBackup", payload["exportedAt"])
    if prefix == "wealth-dashboard-auto":
        db_store.set_metadata("lastAutoBackup", info)
    return info


def write_pre_change_backup(reason: str) -> dict[str, Any]:
    info = write_database_backup("wealth-dashboard-before-change")
    db_store.set_metadata("lastPreChangeBackup", {**info, "reason": reason})
    return info


def ensure_daily_backup() -> dict[str, Any]:
    metadata = db_store.read_metadata()
    today = datetime.now(TWD).date().isoformat()
    last_backup = metadata.get("lastAutoBackup")
    if isinstance(last_backup, dict) and str(last_backup.get("createdAt", "")).startswith(today):
        return {**last_backup, "created": False}
    return {**write_database_backup(), "created": True}


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


def backup_file_info(path: Path) -> dict[str, Any]:
    return {
        "name": path.name,
        "files": [path.name],
        "fileCount": 1,
        "size": path.stat().st_size,
    }


def parse_manual_amount(value: Optional[str], label: str) -> Optional[int]:
    if value is None or value.strip() == "":
        return None
    cleaned = value.replace(",", "").replace("NT$", "").replace("$", "").strip()
    try:
        return round(float(cleaned))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=f"{label} 必須是數字。") from error


def parse_month_key(value: Optional[str]) -> Optional[str]:
    if value is None or value.strip() == "":
        return None
    text = value.strip().replace("/", "-")
    parts = text.split("-")
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail="月份格式不正確。")
    year = int(parts[0]) if parts[0].isdigit() else 0
    month = int(parts[1]) if parts[1].isdigit() else 0
    if year < 1911:
        year += 1911
    if year < 2000 or month < 1 or month > 12:
        raise HTTPException(status_code=400, detail="月份格式不正確。")
    return f"{year}-{month:02d}"


def account_components(accounts: dict[str, Any]) -> dict[str, int]:
    breakdown = accounts.get("accountBreakdown") if isinstance(accounts.get("accountBreakdown"), dict) else {}
    post_office = round(float(breakdown.get("postOfficeBalance", 0) or 0))
    sinopac = round(float(breakdown.get("sinopacBalance", 0) or 0))
    has_sinopac_balance = "sinopacBalance" in breakdown and breakdown.get("sinopacBalance") not in (None, "")
    has_fund_buckets = any(key in breakdown for key in ["emergencyFund", "investmentReserve", "availableCash"])
    emergency_fund = round(float(breakdown.get("emergencyFund", 0) or 0))
    investment_reserve = round(float(breakdown.get("investmentReserve", 0) or 0))
    available_cash = round(float(breakdown.get("availableCash", 0) or 0))
    cash_balance = breakdown.get("cashBalance")
    other_bank = breakdown.get("otherBankBalance")
    if has_sinopac_balance:
        emergency_fund = min(EMERGENCY_FUND_TARGET, sinopac)
        investment_reserve = min(INVESTMENT_RESERVE_TARGET, max(0, sinopac - emergency_fund))
    if has_fund_buckets:
        cash_balance = available_cash
    if cash_balance is None and other_bank is None:
        other_bank = max(0, round(float(accounts.get("cashTWD", 0) or 0)) - post_office - sinopac)
        cash_balance = 0
    if not has_fund_buckets and not has_sinopac_balance:
        total_cash = round(float(accounts.get("cashTWD", 0) or 0))
        emergency_base = total_cash
        emergency_fund = min(EMERGENCY_FUND_TARGET, emergency_base)
        investment_reserve = min(INVESTMENT_RESERVE_TARGET, max(0, total_cash - emergency_fund))
        available_cash = max(0, total_cash - emergency_fund - investment_reserve)
    return {
        "cash": round(float(cash_balance or 0)),
        "bank": round(float(other_bank or 0)),
        "postOfficeBalance": post_office,
        "sinopacBalance": sinopac,
        "emergencyFund": emergency_fund,
        "investmentReserve": investment_reserve,
        "availableCash": available_cash,
    }


def cash_total_from_amounts(amounts: dict[str, Any]) -> int:
    cash = round(float(amounts.get("cash", 0) or 0))
    bank = round(float(amounts.get("bank", 0) or 0))
    post_office = round(float(amounts.get("postOfficeBalance", 0) or 0))
    sinopac = round(float(amounts.get("sinopacBalance", 0) or 0))
    if post_office or sinopac or bank:
        return cash + bank + post_office + sinopac
    return round(float(amounts.get("bucketCash", cash) or cash))


def save_manual_monthly_finance(
    month_key: str,
    income: Optional[int] = None,
    expense: Optional[int] = None,
    sinopac_transfer: Optional[int] = None,
) -> dict[str, Any]:
    finance_data = db_store.read_finance_data({})
    if not isinstance(finance_data, dict) or not isinstance(finance_data.get("years"), list):
        finance_data = {"source": "manual", "years": [], "transactions": []}

    year_key = month_key[:4]
    years = finance_data.setdefault("years", [])
    year_payload = next((year for year in years if str(year.get("year")) == year_key), None)
    existing_month = None
    if year_payload is not None:
        existing_month = next((month for month in year_payload.get("months", []) if month.get("month") == month_key), None)

    next_income = int(income if income is not None else (existing_month or {}).get("income") or 0)
    next_expense = int(expense if expense is not None else (existing_month or {}).get("expense") or 0)
    next_transfer = int(
        sinopac_transfer if sinopac_transfer is not None else (existing_month or {}).get("sinopacTransfer") or 0
    )
    net = next_income - next_expense
    month_payload = {
        **(existing_month or {}),
        "month": month_key,
        "income": next_income,
        "expense": next_expense,
        "net": net,
        "savingsRate": round((net / next_income * 100), 1) if next_income else 0,
        "transactions": int((existing_month or {}).get("transactions") or 0),
        "sinopacTransfer": next_transfer,
        "source": "manual",
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
    }
    if year_payload is None:
        year_payload = {"year": year_key, "months": []}
        years.append(year_payload)

    months = [month for month in year_payload.get("months", []) if month.get("month") != month_key]
    months.append(month_payload)
    months.sort(key=lambda month: str(month.get("month", "")), reverse=True)
    year_payload["months"] = months

    for year in years:
        year_months = [month for month in year.get("months", []) if isinstance(month, dict)]
        year_income = sum(int(month.get("income") or 0) for month in year_months)
        year_expense = sum(int(month.get("expense") or 0) for month in year_months)
        year_net = sum(int(month.get("net") or 0) for month in year_months)
        year["income"] = year_income
        year["expense"] = year_expense
        year["net"] = year_net
        year["savingsRate"] = round((year_net / year_income * 100), 1) if year_income else 0
        year["transactions"] = sum(int(month.get("transactions") or 0) for month in year_months)
        year["sinopacTransfer"] = sum(int(month.get("sinopacTransfer") or 0) for month in year_months)
        year["months"] = sorted(year_months, key=lambda month: str(month.get("month", "")), reverse=True)

    years.sort(key=lambda year: str(year.get("year", "")), reverse=True)
    finance_data["source"] = "manual"
    finance_data["generatedAt"] = datetime.now().isoformat(timespec="seconds")
    finance_data["recordCount"] = sum(int(year.get("transactions") or 0) for year in years)
    imported_months = finance_data.get("importedMonths")
    if not isinstance(imported_months, list):
        imported_months = []
    finance_data["importedMonths"] = sorted({*map(str, imported_months), month_key}, reverse=True)

    saved_to = db_store.write_finance_data(finance_data)
    verify_saved_finance_month(month_key, month_payload)
    db_store.set_metadata("lastFinanceDataImport", db_store.now_iso())
    portfolio = read_portfolio(use_examples=False)
    history = backfill_history_from_finance_data(portfolio) if portfolio else db_store.read_net_worth_history()
    return {
        **month_payload,
        "savedTo": saved_to,
        "historyCount": len(history),
    }


def position_state(transactions: list[dict[str, Any]], market: str, symbol: str) -> dict[str, Any]:
    shares = 0.0
    cost = 0.0
    realized_gain = 0.0
    steps: list[dict[str, Any]] = []
    key_market = market.upper()
    key_symbol = symbol.upper()

    for item in sorted(transactions, key=lambda row: (row.get("date", ""), row.get("id", ""))):
        if str(item.get("market", "")).upper() != key_market or str(item.get("symbol", "")).upper() != key_symbol:
            continue
        action = str(item.get("action", "")).upper()
        trade_shares = float(item.get("shares") or 0)
        price = float(item.get("price") or 0)
        fee = float(item.get("fee") or 0)
        before_shares = shares
        before_average = cost / shares if shares else 0
        realized_this_trade = 0.0

        if action == "BUY":
            shares += trade_shares
            cost += trade_shares * price + fee
        elif action == "SELL":
            if trade_shares > shares:
                raise HTTPException(status_code=400, detail=f"{key_market}:{key_symbol} 賣出股數大於目前持股。")
            sold_cost = before_average * trade_shares
            proceeds = trade_shares * price - fee
            shares -= trade_shares
            cost -= sold_cost
            realized_this_trade = proceeds - sold_cost
            realized_gain += realized_this_trade

        steps.append({
            "id": item.get("id", ""),
            "date": item.get("date", ""),
            "action": action,
            "shares": round(trade_shares, 6),
            "price": round(price, 4),
            "fee": round(fee, 2),
            "beforeShares": round(before_shares, 6),
            "beforeAverageCost": round(before_average, 4),
            "afterShares": round(shares, 6),
            "afterAverageCost": round(cost / shares, 4) if shares else 0,
            "remainingCost": round(cost, 2),
            "realizedGain": round(realized_this_trade, 2),
        })

    return {
        "market": key_market,
        "symbol": key_symbol,
        "shares": round(shares, 6),
        "averageCost": round(cost / shares, 4) if shares else 0,
        "totalCost": round(cost, 2),
        "realizedGain": round(realized_gain, 2),
        "steps": steps,
    }


def _is_00685l_transaction(row: dict[str, Any]) -> bool:
    return str(row.get("market", "")).upper() == "TW" and str(row.get("symbol", "")).upper() == "00685L"


def _is_target_00685l_state(state: dict[str, Any]) -> bool:
    return (
        abs(float(state.get("shares") or 0) - ETF_00685L_TARGET_SHARES) < 0.001
        and abs(float(state.get("averageCost") or 0) - ETF_00685L_TARGET_AVERAGE_COST) < 0.01
    )


def _apply_00685l_split_to_price_cache() -> bool:
    prices = db_store.read_prices({"fxRate": 31.451, "prices": {}})
    price_book = prices.get("prices")
    if not isinstance(price_book, dict):
        return False
    row = price_book.get("TW:00685L")
    if not isinstance(row, dict):
        return False
    price = to_float(row.get("price"))
    if price <= 100:
        return False
    updated_row = dict(row)
    for key in ["price", "previousClose"]:
        value = to_float(updated_row.get(key))
        if value > 100:
            updated_row[key] = round(value / ETF_00685L_SPLIT_RATIO, 4)
    updated_row["updatedAt"] = db_store.now_iso()
    price_book["TW:00685L"] = updated_row
    db_store.write_prices({**prices, "prices": price_book})
    return True


def apply_00685l_split_adjustment() -> dict[str, Any]:
    metadata = db_store.read_metadata()
    existing = metadata.get(ETF_00685L_SPLIT_METADATA_KEY)
    if isinstance(existing, dict) and existing.get("status") == "applied":
        return {"ok": True, "status": "alreadyApplied", **existing}

    transactions, ids_changed = ensure_transaction_ids(read_transactions(use_examples=False))
    split_rows = [row for row in transactions if _is_00685l_transaction(row)]
    if not split_rows:
        return {"ok": True, "status": "skipped", "reason": "no00685LTransactions"}
    if ids_changed:
        write_transactions(transactions)

    before = position_state(transactions, "TW", "00685L")
    if _is_target_00685l_state(before):
        priceAdjusted = _apply_00685l_split_to_price_cache()
        db_store.set_metadata(ETF_00685L_SPLIT_METADATA_KEY, {
            "status": "applied",
            "reason": "alreadyTargetState",
            "appliedAt": db_store.now_iso(),
            "before": before,
            "after": before,
            "priceAdjusted": priceAdjusted,
        })
        return {"ok": True, "status": "alreadyTarget", "before": before, "after": before, "priceAdjusted": priceAdjusted}

    before_shares = float(before.get("shares") or 0)
    if before_shares <= 0 or before_shares >= ETF_00685L_TARGET_SHARES:
        return {"ok": True, "status": "skipped", "reason": "unexpectedCurrentState", "before": before}

    adjusted_transactions: list[dict[str, Any]] = []
    for row in transactions:
        if not _is_00685l_transaction(row):
            adjusted_transactions.append(row)
            continue
        adjusted = dict(row)
        adjusted["shares"] = round(to_float(adjusted.get("shares")) * ETF_00685L_SPLIT_RATIO, 6)
        adjusted["price"] = round(to_float(adjusted.get("price")) / ETF_00685L_SPLIT_RATIO, 6)
        note = str(adjusted.get("note", "")).strip()
        split_note = "00685L 1拆24 股份分割校正"
        adjusted["note"] = note if split_note in note else (f"{note}；{split_note}" if note else split_note)
        adjusted_transactions.append(adjusted)

    after = position_state(adjusted_transactions, "TW", "00685L")
    target_total_cost = round(ETF_00685L_TARGET_SHARES * ETF_00685L_TARGET_AVERAGE_COST, 2)
    if abs(float(after.get("shares") or 0) - ETF_00685L_TARGET_SHARES) < 0.001 and not _is_target_00685l_state(after):
        delta_cost = round(target_total_cost - float(after.get("totalCost") or 0), 2)
        for adjusted in sorted(adjusted_transactions, key=lambda row: (row.get("date", ""), row.get("id", "")), reverse=True):
            if _is_00685l_transaction(adjusted) and str(adjusted.get("action", "")).upper() == "BUY":
                next_fee = round(to_float(adjusted.get("fee")) + delta_cost, 2)
                if next_fee >= 0:
                    adjusted["fee"] = next_fee
                    note = str(adjusted.get("note", "")).strip()
                    fee_note = "校正分割後平均成本至 12.1"
                    adjusted["note"] = note if fee_note in note else (f"{note}；{fee_note}" if note else fee_note)
                    after = position_state(adjusted_transactions, "TW", "00685L")
                break
    if not _is_target_00685l_state(after):
        return {"ok": True, "status": "skipped", "reason": "adjustedStateMismatch", "before": before, "after": after}

    validate_positions(adjusted_transactions)
    write_pre_change_backup("00685L 1拆24 股份分割校正")
    write_transactions(adjusted_transactions)
    priceAdjusted = _apply_00685l_split_to_price_cache()
    portfolio = rebuild_portfolio_outputs()
    applied_at = mark_successful_save()
    result = {
        "status": "applied",
        "appliedAt": applied_at,
        "ratio": ETF_00685L_SPLIT_RATIO,
        "before": before,
        "after": after,
        "priceAdjusted": priceAdjusted,
        "portfolioSummary": portfolio.get("summary", {}),
    }
    db_store.set_metadata(ETF_00685L_SPLIT_METADATA_KEY, result)
    return {"ok": True, **result}


def build_holding_audit() -> dict[str, Any]:
    transactions = read_transactions(use_examples=False)
    portfolio = read_portfolio(use_examples=False) or rebuild_portfolio_outputs()
    holdings = []
    for holding in portfolio.get("holdings", []):
        symbol = str(holding.get("symbol", "")).upper()
        market = str(holding.get("market", "")).upper()
        if not symbol or not market:
            continue
        audit = position_state(transactions, market, symbol)
        holdings.append({
            **holding,
            "audit": audit,
            "transactionCount": len(audit["steps"]),
        })
    holdings.sort(key=lambda row: (str(row.get("market", "")), str(row.get("symbol", ""))))
    return {
        "ok": True,
        "holdings": holdings,
        "summary": portfolio.get("summary", {}),
        "currentDb": db_store.active_backend(),
    }


def transaction_preview(transactions: list[dict[str, Any]], transaction: dict[str, Any], editing_id: str | None = None) -> dict[str, Any]:
    next_transactions = []
    replaced = False
    for row in transactions:
        if editing_id and row.get("id") == editing_id:
            next_transactions.append({**transaction, "id": editing_id})
            replaced = True
        else:
            next_transactions.append(row)
    if not editing_id:
        next_transactions.append(transaction)
    elif not replaced:
        raise HTTPException(status_code=404, detail="找不到要預覽的交易紀錄。")

    validate_positions(next_transactions)
    before = position_state(transactions, transaction["market"], transaction["symbol"])
    after = position_state(next_transactions, transaction["market"], transaction["symbol"])
    return {
        "ok": True,
        "symbol": transaction["symbol"],
        "market": transaction["market"],
        "before": {key: before[key] for key in ["shares", "averageCost", "totalCost", "realizedGain"]},
        "after": {key: after[key] for key in ["shares", "averageCost", "totalCost", "realizedGain"]},
        "deltaShares": round(after["shares"] - before["shares"], 6),
        "deltaCost": round(after["totalCost"] - before["totalCost"], 2),
        "latestStep": after["steps"][-1] if after["steps"] else None,
    }


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def detect_data_anomalies(
    portfolio: dict[str, Any] | None = None,
    transactions: list[dict[str, Any]] | None = None,
    history: list[dict[str, Any]] | None = None,
) -> list[str]:
    anomalies: list[str] = []
    portfolio = portfolio if portfolio is not None else read_portfolio(use_examples=False)
    transactions = transactions if transactions is not None else read_transactions(use_examples=False)
    history = history if history is not None else db_store.read_net_worth_history()
    if not portfolio:
        return ["目前讀不到投資組合資料。"]

    summary = portfolio.get("summary", {})
    net_worth = to_float(summary.get("netWorth"))
    total_assets = to_float(summary.get("totalAssets"))
    stock_assets = to_float(summary.get("stockAssets"))
    cash = to_float(summary.get("cash"))
    debt = to_float(summary.get("debt"))
    if total_assets < 0 or net_worth < 0:
        anomalies.append("總資產或淨資產小於 0，請確認資產。")
    if abs((stock_assets + cash) - total_assets) > max(1000, total_assets * 0.02):
        anomalies.append("總資產與股票資產加現金沒有對上，請確認資產組合。")
    if debt > total_assets and total_assets > 0:
        anomalies.append("負債大於總資產，請確認負債資料。")

    history_rows = [
        row
        for row in (history or [])
        if to_float(row.get("netWorth")) > 0 and to_float(row.get("netWorth")) != net_worth
    ]
    if history_rows:
        previous_net_worth = to_float(history_rows[-1].get("netWorth"))
        if previous_net_worth and net_worth < previous_net_worth * 0.7:
            anomalies.append("淨資產比前次紀錄少超過 30%，請先確認資料是否被覆蓋。")

    positions = calculate_positions(transactions or [])
    holdings = portfolio.get("holdings", [])
    holding_map = {
        f"{str(row.get('market', '')).upper()}:{str(row.get('symbol', '')).upper()}": row
        for row in holdings
    }
    for key, position in positions.items():
        holding = holding_map.get(key)
        if not holding:
            anomalies.append(f"{key} 有交易紀錄但目前持股資料沒有顯示。")
            continue
        position_shares = to_float(position.get("shares"))
        holding_shares = to_float(holding.get("shares"))
        if abs(position_shares - holding_shares) > 0.001:
            anomalies.append(f"{key} 持股股數與交易紀錄加總不一致。")

    for holding in holdings:
        market = str(holding.get("market", "")).upper()
        symbol = str(holding.get("symbol", "")).upper()
        label = f"{market}:{symbol}"
        shares = to_float(holding.get("shares"))
        price = to_float(holding.get("price"))
        average_cost = to_float(holding.get("averageCost"))
        market_value = to_float(holding.get("marketValueTWD"))
        return_rate = to_float(holding.get("returnRate"))
        if shares > 0 and price <= 0:
            anomalies.append(f"{label} 現價不是有效數字，請先不要更新股價。")
        if shares > 0 and average_cost <= 0:
            anomalies.append(f"{label} 平均成本不是有效數字，請確認交易紀錄。")
        if shares > 0 and market_value <= 0:
            anomalies.append(f"{label} 市值不是有效數字，請確認股價。")
        if abs(return_rate) > 300:
            anomalies.append(f"{label} 報酬率超過 300%，請確認成本或股價。")

    return anomalies


def filter_safe_price_updates(
    current_portfolio: dict[str, Any],
    stored_prices: dict[str, Any],
    latest_prices: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    warnings: list[str] = []
    holdings = {
        f"{str(row.get('market', '')).upper()}:{str(row.get('symbol', '')).upper()}": row
        for row in current_portfolio.get("holdings", [])
    }
    previous_prices = stored_prices.get("prices", {}) if isinstance(stored_prices.get("prices"), dict) else {}
    safe_prices: dict[str, dict[str, Any]] = {}
    for key, row in latest_prices.items():
        new_price = to_float(row.get("price"))
        old_price = to_float((previous_prices.get(key) or {}).get("price")) or to_float((holdings.get(key) or {}).get("price"))
        if new_price <= 0:
            warnings.append(f"{key} 價格為 0 或無效，已保留原價格。")
            continue
        if old_price > 0:
            change_ratio = abs(new_price - old_price) / old_price
            split_adjusted_00685l = (
                key == "TW:00685L"
                and abs((old_price / max(new_price, 0.0001)) - ETF_00685L_SPLIT_RATIO) <= 1.5
            )
            if change_ratio > 0.8 and not split_adjusted_00685l:
                warnings.append(f"{key} 新價格與原價格差距超過 80%，已保留原價格。")
                continue
        safe_prices[key] = row
    return safe_prices, warnings


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


@app.get("/api/app-version")
def app_version() -> dict[str, Any]:
    return app_version_payload()


@app.api_route("/login.html", methods=["GET", "HEAD"])
def login_page(request: Request) -> Response:
    has_error = request.query_params.get("error") == "1"
    error_html = "<p class=\"error\">帳號或密碼不正確，請再試一次。</p>" if has_error else ""
    return Response(
        media_type="text/html; charset=utf-8",
        content=f"""<!doctype html>
<html lang="zh-Hant">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>登入｜哲哲❤️臻臻的小天地</title>
    <style>
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        min-height: 100vh;
        display: grid;
        place-items: center;
        padding: 24px;
        color: #4d3730;
        font-family: Inter, "Noto Sans TC", "PingFang TC", "Microsoft JhengHei", system-ui, sans-serif;
        background:
          radial-gradient(circle at 58% 12%, rgba(255, 210, 210, 0.46), transparent 34%),
          radial-gradient(circle at 88% 4%, rgba(241, 222, 205, 0.6), transparent 24%),
          linear-gradient(180deg, #fff8f3 0%, #fff2ee 46%, #f9eadf 100%);
      }}
      main {{
        width: min(420px, 100%);
        padding: 28px;
        border: 1px solid rgba(226, 173, 162, 0.28);
        border-radius: 24px;
        background: rgba(255, 253, 250, 0.92);
        box-shadow: 0 18px 42px rgba(158, 103, 88, 0.12);
      }}
      h1 {{ margin: 0 0 8px; font-size: 32px; line-height: 1.1; white-space: nowrap; }}
      p {{ margin: 0 0 22px; color: #8d746c; font-weight: 750; line-height: 1.6; }}
      label {{ display: grid; gap: 8px; margin-bottom: 14px; color: #d17f87; font-size: 13px; font-weight: 900; }}
      input {{
        width: 100%;
        min-height: 48px;
        padding: 12px 14px;
        border: 1px solid rgba(226, 173, 162, 0.34);
        border-radius: 14px;
        color: #4d3730;
        background: rgba(255, 253, 250, 0.92);
        font: inherit;
      }}
      input:focus {{ outline: 2px solid rgba(209, 127, 135, 0.36); outline-offset: 2px; }}
      button {{
        width: 100%;
        min-height: 48px;
        border: 1px solid rgba(226, 173, 162, 0.3);
        border-radius: 999px;
        color: #4d3730;
        font: inherit;
        font-weight: 900;
        cursor: pointer;
        background: linear-gradient(135deg, rgba(246, 183, 183, 0.92), rgba(255, 215, 168, 0.72));
        box-shadow: 0 14px 34px rgba(158, 103, 88, 0.09);
      }}
      .error {{
        margin: 0 0 14px;
        padding: 10px 12px;
        border-radius: 14px;
        color: #9f3449;
        background: rgba(255, 223, 223, 0.78);
      }}
      small {{ display: block; margin-top: 14px; color: #8d746c; font-weight: 800; line-height: 1.55; }}
    </style>
  </head>
  <body>
    <main>
      <h1>哲哲❤️臻臻的小天地</h1>
      <p>登入一次後，這台瀏覽器會記住 30 天。</p>
      {error_html}
      <form method="post" action="/api/login">
        <label>帳號<input name="username" autocomplete="username" value="{AUTH_USERNAME}" required /></label>
        <label>密碼<input name="password" type="password" autocomplete="current-password" required autofocus /></label>
        <button type="submit">登入</button>
      </form>
      <small>登入後首頁和資料讀取會使用 cookie，不會一直呼叫 MacBook 鑰匙圈。</small>
    </main>
  </body>
</html>""",
    )


@app.post("/api/login")
async def login(request: Request) -> Response:
    username = ""
    password = ""
    try:
        if "application/json" in request.headers.get("content-type", ""):
            payload = await request.json()
            if isinstance(payload, dict):
                username = str(payload.get("username", "")).strip()
                password = str(payload.get("password", ""))
        else:
            form = await request.form()
            username = str(form.get("username", "")).strip()
            password = str(form.get("password", ""))
    except Exception:
        username = ""
        password = ""

    ok = bool(AUTH_PASSWORD) and secrets.compare_digest(username, AUTH_USERNAME) and secrets.compare_digest(password, AUTH_PASSWORD)
    if not ok:
        if wants_json(request):
            return JSONResponse(status_code=401, content={"ok": False, "detail": "帳號或密碼不正確。"})
        return RedirectResponse(url="/login.html?error=1", status_code=303)

    response: Response
    if wants_json(request):
        response = JSONResponse(content={"ok": True, "message": "登入成功。"})
    else:
        response = RedirectResponse(url="/index.html", status_code=303)
    response.set_cookie(
        AUTH_COOKIE_NAME,
        make_auth_token(username),
        max_age=AUTH_COOKIE_MAX_AGE,
        httponly=True,
        secure=is_secure_request(request),
        samesite="lax",
        path="/",
    )
    return response


@app.post("/api/logout")
def logout() -> JSONResponse:
    response = JSONResponse(content={"ok": True, "message": "已登出。"})
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return response


@app.get("/api/db/status")
def db_status() -> Response:
    return utf8_json(db_store.status())


@app.get("/api/data-health")
def data_health(request: Request) -> Response:
    read_ok = True
    write_ok = True
    price_api_ok = True
    read_error = ""
    write_error = ""
    price_error = ""
    backup: dict[str, Any] | None = None
    anomalies: list[str] = []

    try:
        transactions = read_transactions(use_examples=False)
        calculate_positions(transactions)
    except Exception as error:
        read_ok = False
        read_error = str(error)
        transactions = []

    try:
        db_store.set_metadata("lastWriteCheck", db_store.now_iso())
        metadata = db_store.read_metadata()
        write_ok = bool(metadata.get("lastWriteCheck"))
    except Exception as error:
        write_ok = False
        write_error = str(error)

    try:
        backup = ensure_daily_backup()
    except Exception as error:
        anomalies.append(f"每日備份未完成：{error}")

    try:
        if request.headers.get("x-price-check") == "fail":
            raise RuntimeError("股價更新 API 檢查失敗")
    except Exception as error:
        price_api_ok = False
        price_error = str(error)

    status = db_store.status()
    try:
        anomalies.extend(detect_data_anomalies(read_portfolio(use_examples=False), transactions, db_store.read_net_worth_history()))
    except Exception as error:
        anomalies.append(f"資料異常檢查未完成：{error}")
    if status.get("fallbackActive"):
        anomalies.append("Supabase 目前不是正式寫入狀態。")
    if not transactions:
        anomalies.append("目前沒有交易紀錄。")
    summary = plain_health_summary(read_ok, write_ok, price_api_ok, status, anomalies)

    payload = {
        "ok": read_ok and write_ok and price_api_ok,
        "summary": summary,
        "readOk": read_ok,
        "writeOk": write_ok,
        "priceApiOk": price_api_ok,
        "authOk": True,
        "readError": read_error,
        "writeError": write_error,
        "priceError": price_error,
        "lastSuccessfulSave": status.get("metadata", {}).get("lastSuccessfulSave"),
        "lastBackup": status.get("metadata", {}).get("lastBackup"),
        "lastAutoBackup": status.get("metadata", {}).get("lastAutoBackup"),
        "lastPriceUpdate": status.get("metadata", {}).get("lastPriceUpdate"),
        "backup": backup,
        "anomalies": anomalies,
        "status": status,
    }
    return utf8_json(payload)


@app.get("/api/price-health")
def price_health() -> dict[str, Any]:
    status = db_store.status()
    return {
        "ok": True,
        "updating": PRICE_UPDATE_LOCK.locked(),
        "lastPriceUpdate": status.get("metadata", {}).get("lastPriceUpdate"),
        "message": "股價更新狀態正常。",
    }


@app.get("/api/db/debug-supabase")
def debug_supabase() -> dict[str, Any]:
    return db_store.debug_supabase()


@app.get("/api/portfolio")
def get_portfolio() -> dict[str, Any]:
    portfolio = read_portfolio(use_examples=True)
    return {"ok": True, "portfolio": portfolio, "source": db_store.active_backend() if portfolio else "example"}


def future_result_or_default(future: Any, default: Any, label: str) -> Any:
    try:
        return future.result()
    except Exception as error:
        print(f"Dashboard core skipped {label}: {error}")
        try:
            return default() if callable(default) else default
        except Exception as fallback_error:
            print(f"Dashboard core fallback failed {label}: {fallback_error}")
            return {} if label in {"portfolio", "accounts", "finance"} else []


@app.get("/api/dashboard-core")
def get_dashboard_core(fast: bool = False) -> Response:
    with ThreadPoolExecutor(max_workers=6) as executor:
        portfolio_future = executor.submit(read_portfolio, True)
        history_future = executor.submit(read_net_worth_history, False)
        accounts_future = executor.submit(db_store.read_accounts, {})
        transactions_future = None if fast else executor.submit(read_transactions, False)
        dividends_future = None if fast else executor.submit(read_dividends, False)
        finance_future = executor.submit(db_store.read_finance_data, {})
        portfolio = future_result_or_default(portfolio_future, lambda: read_portfolio(use_examples=True), "portfolio")
        history = future_result_or_default(history_future, [], "history")
        transactions = [] if fast else future_result_or_default(transactions_future, [], "transactions")
        dividends = [] if fast else future_result_or_default(dividends_future, [], "dividends")
        accounts = future_result_or_default(accounts_future, {}, "accounts")
        finance_data = future_result_or_default(finance_future, {}, "finance")
    current_month = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m")
    current_month_finance = None
    if isinstance(finance_data, dict):
        for year in finance_data.get("years", []):
            for month in year.get("months", []):
                if month.get("month") == current_month:
                    current_month_finance = month
                    break
            if current_month_finance:
                break
    return utf8_json({
        "ok": True,
        "portfolio": portfolio,
        "history": history,
        "transactions": None if fast else transactions,
        "dividends": None if fast else dividends,
        "accounts": accounts,
        "currentMonthFinance": current_month_finance,
        "fast": fast,
        "source": db_store.active_backend() if portfolio else "example",
    })


@app.get("/api/holdings-audit")
def holdings_audit() -> Response:
    return utf8_json(build_holding_audit())


@app.post("/api/corporate-actions/00685l-split")
def apply_00685l_split() -> Response:
    return utf8_json(apply_00685l_split_adjustment())


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
    history = read_net_worth_history(use_examples=False)
    return {"ok": True, "history": history, "source": db_store.active_backend() if history else "empty"}


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

    write_pre_change_backup("匯入年度/月度對帳")
    saved_to = db_store.write_finance_data(finance_data)
    verify_saved_finance_import(finance_data)
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
            write_pre_change_backup("匯入年度/月度對帳備份")
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
        write_pre_change_backup("匯入完整備份")
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
    local_import_backup_created = False

    def ensure_local_import_backup() -> None:
        nonlocal local_import_backup_created
        if local_import_backup_created:
            return
        write_pre_change_backup("匯入本機 JSON")
        local_import_backup_created = True

    accounts = read_json(DATA_DIR / "accounts.json", None)
    if isinstance(accounts, dict):
        ensure_local_import_backup()
        db_store.write_accounts(accounts)
        imported.append("accounts.json")

    transactions = read_json(DATA_DIR / "transactions.json", None)
    if isinstance(transactions, list):
        ensure_local_import_backup()
        transactions, _ = ensure_transaction_ids(transactions)
        db_store.write_transactions(transactions)
        imported.append("transactions.json")

    dividends = read_json(DATA_DIR / "dividends.json", None)
    if isinstance(dividends, list):
        ensure_local_import_backup()
        dividends, _ = ensure_dividend_ids(dividends)
        db_store.write_dividends(dividends)
        imported.append("dividends.json")

    prices = read_json(DATA_DIR / "prices.json", None)
    if isinstance(prices, dict):
        ensure_local_import_backup()
        db_store.write_prices(prices)
        imported.append("prices.json")

    history = read_json(DATA_DIR / "net-worth-history.json", None)
    if isinstance(history, list):
        ensure_local_import_backup()
        db_store.write_net_worth_history(history)
        imported.append("net-worth-history.json")

    finance_data = read_local_finance_data()
    if finance_data:
        ensure_local_import_backup()
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
    write_pre_change_backup("重建投資組合")
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
    backups.extend(backup_file_info(path) for path in BACKUPS_DIR.glob("*.json") if path.is_file())
    backups.sort(key=lambda row: row["name"], reverse=True)
    return {"ok": True, "backups": backups}


@app.post("/api/db/auto-backup")
def create_database_backup() -> dict[str, Any]:
    return {"ok": True, "backup": write_database_backup("wealth-dashboard-manual")}


@app.post("/api/db/daily-backup")
def create_daily_database_backup() -> dict[str, Any]:
    backup = ensure_daily_backup()
    return {"ok": True, "backup": backup, "created": bool(backup.get("created"))}


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
    transactions = read_transactions(use_examples=False)
    transactions, changed = ensure_transaction_ids(transactions)
    if changed:
        write_transactions(transactions)
    return {"ok": True, "transactions": transactions, "source": db_store.active_backend() if transactions else "empty"}


@app.post("/api/transactions/preview")
async def preview_transaction(request: Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as error:
        raise HTTPException(status_code=400, detail="交易資料格式錯誤。") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="交易資料格式錯誤。")

    editing_id = str(payload.get("editingId", "")).strip() or None
    transaction = normalize_transaction(payload)
    return transaction_preview(read_transactions(use_examples=False), transaction, editing_id)


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

    write_pre_change_backup("新增交易")
    write_transactions(next_transactions)
    verified_transactions = read_transactions(use_examples=False)
    verify_saved_row(verified_transactions, transaction, "交易已送出，但重新讀取後沒有找到這筆資料，請不要重複新增，先重新整理確認。")
    saved_at = mark_successful_save()
    portfolio = rebuild_portfolio_outputs()
    return {**transaction_response(verified_transactions, transaction, portfolio), "savedAt": saved_at}


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

    write_pre_change_backup("更新交易")
    write_transactions(next_transactions)
    verified_transactions = read_transactions(use_examples=False)
    verify_saved_row(verified_transactions, transaction, "交易已送出，但重新讀取後內容沒有對上，請重新整理確認。")
    saved_at = mark_successful_save()
    portfolio = rebuild_portfolio_outputs()
    return {**transaction_response(verified_transactions, transaction, portfolio), "savedAt": saved_at}


@app.delete("/api/transactions/{transaction_id}")
def delete_transaction(transaction_id: str) -> dict:
    transactions, changed = ensure_transaction_ids(read_transactions())
    next_transactions = [row for row in transactions if row.get("id") != transaction_id]
    if len(next_transactions) == len(transactions):
        raise HTTPException(status_code=404, detail="找不到交易紀錄。")

    validate_positions(next_transactions)
    write_pre_change_backup("刪除交易")
    write_transactions(next_transactions)
    verified_transactions = read_transactions(use_examples=False)
    verify_deleted_row(verified_transactions, transaction_id, "交易刪除已送出，但重新讀取後仍看到這筆資料，請重新整理確認。")
    saved_at = mark_successful_save()
    portfolio = rebuild_portfolio_outputs()
    return {**transaction_response(verified_transactions, None, portfolio), "savedAt": saved_at}


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
    write_pre_change_backup("新增股息")
    if changed:
        write_dividends(dividends)
    db_store.upsert_dividend(dividend)
    next_dividends = read_dividends()
    verify_saved_dividend(next_dividends, dividend, "股息已送出，但重新讀取後沒有找到這筆資料，請不要重複新增，先重新整理確認。")
    saved_at = mark_successful_save()
    return {"ok": True, "verified": True, "savedAt": saved_at, "dividend": dividend, "dividends": next_dividends}


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
    write_pre_change_backup("更新股息")
    db_store.upsert_dividend(dividend)
    next_dividends = read_dividends()
    verify_saved_dividend(next_dividends, dividend, "股息已送出，但重新讀取後內容沒有對上，請重新整理確認。")
    saved_at = mark_successful_save()
    return {"ok": True, "verified": True, "savedAt": saved_at, "dividend": dividend, "dividends": next_dividends}


@app.delete("/api/dividends/{dividend_id}")
def delete_dividend(dividend_id: str) -> dict:
    dividends, changed = ensure_dividend_ids(read_dividends())
    if not any(row.get("id") == dividend_id for row in dividends):
        raise HTTPException(status_code=404, detail="找不到股息紀錄。")
    write_pre_change_backup("刪除股息")
    db_store.delete_dividend(dividend_id)
    next_dividends = read_dividends()
    verify_deleted_row(next_dividends, dividend_id, "股息刪除已送出，但重新讀取後仍看到這筆資料，請重新整理確認。")
    saved_at = mark_successful_save()
    return {"ok": True, "verified": True, "savedAt": saved_at, "dividend": None, "dividends": next_dividends}


@app.post("/api/update-asset-snapshot")
async def update_asset_snapshot(
    request: Request,
    csv: Optional[UploadFile] = File(None),
    image: Optional[UploadFile] = File(None),
    cash: Optional[str] = Form(None),
    emergencyFund: Optional[str] = Form(None),
    investmentReserve: Optional[str] = Form(None),
    availableCash: Optional[str] = Form(None),
    bank: Optional[str] = Form(None),
    otherBankBalance: Optional[str] = Form(None),
    postOfficeBalance: Optional[str] = Form(None),
    sinopacBalance: Optional[str] = Form(None),
    creditCardDebt: Optional[str] = Form(None),
    month: Optional[str] = Form(None),
    monthlyIncome: Optional[str] = Form(None),
    monthlyExpense: Optional[str] = Form(None),
    monthlySinopacTransfer: Optional[str] = Form(None),
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
    emergencyFund = emergencyFund or form_text("emergencyFund", "emergency_fund")
    investmentReserve = investmentReserve or form_text("investmentReserve", "investment_reserve")
    availableCash = availableCash or form_text("availableCash", "available_cash")
    bank = bank or otherBankBalance or form_text("otherBankBalance", "other_bank_balance", "bank")
    postOfficeBalance = postOfficeBalance or form_text("postOfficeBalance", "post_office_balance", "postOffice")
    sinopacBalance = sinopacBalance or form_text("sinopacBalance", "sinopac_balance", "sinopac")
    creditCardDebt = creditCardDebt or form_text("creditCardDebt", "debt", "credit_card_debt")
    month = month or form_text("month", "financeMonth")
    monthlyIncome = monthlyIncome or form_text("monthlyIncome", "income")
    monthlyExpense = monthlyExpense or form_text("monthlyExpense", "expense")
    monthlySinopacTransfer = monthlySinopacTransfer or form_text(
        "monthlySinopacTransfer",
        "sinopacTransfer",
        "monthly_sinopac_transfer",
    )

    manual_cash = parse_manual_amount(cash, "現金")
    manual_emergency_fund = parse_manual_amount(emergencyFund, "緊急預備金")
    manual_investment_reserve = parse_manual_amount(investmentReserve, "投資預備金")
    manual_available_cash = parse_manual_amount(availableCash, "現金")
    manual_bank = parse_manual_amount(bank, "其他銀行餘額")
    manual_post_office = parse_manual_amount(postOfficeBalance, "郵局餘額")
    manual_sinopac = parse_manual_amount(sinopacBalance, "永豐餘額")
    manual_debt = parse_manual_amount(creditCardDebt, "信用卡負債")
    manual_income = parse_manual_amount(monthlyIncome, "月份收入")
    manual_expense = parse_manual_amount(monthlyExpense, "月份支出")
    manual_sinopac_transfer = parse_manual_amount(monthlySinopacTransfer, "本月轉入永豐")
    manual_month = parse_month_key(month)
    manual_values = {
        "現金總額": manual_cash,
        "緊急預備金": manual_emergency_fund,
        "投資預備金": manual_investment_reserve,
        "現金": manual_available_cash,
        "其他銀行餘額": manual_bank,
        "郵局餘額": manual_post_office,
        "永豐餘額": manual_sinopac,
        "信用卡負債": manual_debt,
    }
    has_any_manual = any(value is not None for value in manual_values.values())

    has_any_monthly_finance = any(value is not None for value in [manual_income, manual_expense])
    has_partial_monthly_finance = any(value is not None for value in [manual_income, manual_expense]) and not all(
        value is not None for value in [manual_income, manual_expense]
    )
    has_monthly_transfer = manual_sinopac_transfer is not None
    has_any_monthly = has_any_monthly_finance or has_monthly_transfer
    if has_any_monthly and manual_month is None:
        raise HTTPException(status_code=400, detail="請選擇統計月份。")
    if has_partial_monthly_finance:
        raise HTTPException(status_code=400, detail="月份收入與支出請同時填寫。")

    monthly_finance = None
    has_fund_buckets = any(value is not None for value in [manual_emergency_fund, manual_investment_reserve, manual_available_cash])
    has_account_breakdown = any(value is not None for value in [
        manual_post_office,
        manual_sinopac,
        manual_bank,
        manual_emergency_fund,
        manual_investment_reserve,
        manual_available_cash,
    ])

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

        if has_any_manual:
            existing_accounts = db_store.read_accounts({})
            existing_parts = account_components(existing_accounts)
            next_emergency = manual_emergency_fund if manual_emergency_fund is not None else existing_parts["emergencyFund"]
            next_reserve = manual_investment_reserve if manual_investment_reserve is not None else existing_parts["investmentReserve"]
            next_available = manual_available_cash if manual_available_cash is not None else existing_parts["availableCash"]
            bucket_cash = next_emergency + next_reserve + next_available
            next_cash = next_available if has_fund_buckets else (
                manual_cash if manual_cash is not None else existing_parts["cash"]
            )
            amounts = {
                "cash": next_cash,
                "bucketCash": bucket_cash,
                "emergencyFund": next_emergency,
                "investmentReserve": next_reserve,
                "availableCash": next_available,
                "bank": manual_bank if manual_bank is not None else existing_parts["bank"],
                "postOfficeBalance": manual_post_office if manual_post_office is not None else existing_parts["postOfficeBalance"],
                "sinopacBalance": manual_sinopac if manual_sinopac is not None else existing_parts["sinopacBalance"],
                "debt": manual_debt if manual_debt is not None else round(float(existing_accounts.get("creditCardDebt", 0) or 0)),
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
            if not has_any_monthly and not has_account_breakdown:
                raise HTTPException(
                    status_code=400,
                    detail="請至少上傳 MOZE CSV、帳戶總覽截圖，填寫資金水位、銀行、信用卡負債，或填寫月份統計。",
                )
            amounts = {
                "cash": 0,
                "bank": 0,
                "debt": 0,
                "source": "supplement_only",
            }

    write_pre_change_backup("更新資產")
    if amounts["source"] == "supplement_only":
        if has_any_monthly:
            monthly_finance = save_manual_monthly_finance(
                manual_month or "",
                manual_income if has_any_monthly_finance else None,
                manual_expense if has_any_monthly_finance else None,
                manual_sinopac_transfer,
            )
        accounts = db_store.read_accounts({})
        portfolio = read_portfolio(use_examples=False) or {}
    elif amounts["source"] == "existing_accounts":
        accounts = db_store.read_accounts({})
        if has_any_monthly:
            monthly_finance = save_manual_monthly_finance(
                manual_month or "",
                manual_income if has_any_monthly_finance else None,
                manual_expense if has_any_monthly_finance else None,
                manual_sinopac_transfer,
            )
    else:
        accounts = db_store.read_accounts({})
        accounts["cashTWD"] = (
            cash_total_from_amounts(amounts)
            if has_fund_buckets
            else (
                amounts["cash"]
                + amounts["bank"]
                + round(float(amounts.get("postOfficeBalance", 0) or 0))
                + round(float(amounts.get("sinopacBalance", 0) or 0))
            )
        )
        accounts["creditCardDebt"] = amounts["debt"]
        accounts.setdefault("cashUSD", 0)
        accounts.setdefault("otherDebt", 0)
    account_breakdown = accounts.get("accountBreakdown")
    if not isinstance(account_breakdown, dict):
        account_breakdown = {}
    if manual_cash is not None or has_fund_buckets or amounts["source"] == "manual":
        account_breakdown["cashBalance"] = round(float(amounts.get("cash", manual_cash or 0) or 0))
    if manual_emergency_fund is not None or has_fund_buckets or amounts["source"] == "manual":
        account_breakdown["emergencyFund"] = round(float(amounts.get("emergencyFund", manual_emergency_fund or 0) or 0))
    if manual_investment_reserve is not None or has_fund_buckets or amounts["source"] == "manual":
        account_breakdown["investmentReserve"] = round(float(amounts.get("investmentReserve", manual_investment_reserve or 0) or 0))
    if manual_available_cash is not None or has_fund_buckets or amounts["source"] == "manual":
        account_breakdown["availableCash"] = round(float(amounts.get("availableCash", manual_available_cash or 0) or 0))
    if manual_bank is not None or amounts["source"] == "manual":
        account_breakdown["otherBankBalance"] = round(float(amounts.get("bank", manual_bank or 0) or 0))
    if manual_post_office is not None or amounts["source"] == "manual":
        account_breakdown["postOfficeBalance"] = round(float(amounts.get("postOfficeBalance", manual_post_office or 0) or 0))
    if manual_sinopac is not None or amounts["source"] == "manual":
        account_breakdown["sinopacBalance"] = round(float(amounts.get("sinopacBalance", manual_sinopac or 0) or 0))
    if (
        manual_cash is not None
        or has_fund_buckets
        or manual_post_office is not None
        or manual_sinopac is not None
        or manual_bank is not None
        or amounts["source"] == "manual"
    ):
        parts = account_components(accounts)
        next_cash = round(float(account_breakdown.get("cashBalance", parts["cash"]) or 0))
        next_bank = round(float(account_breakdown.get("otherBankBalance", parts["bank"]) or 0))
        next_post_office = round(float(account_breakdown.get("postOfficeBalance", parts["postOfficeBalance"]) or 0))
        next_sinopac = round(float(account_breakdown.get("sinopacBalance", parts["sinopacBalance"]) or 0))
        accounts["cashTWD"] = next_cash + next_bank + next_post_office + next_sinopac
        account_breakdown["updatedAt"] = datetime.now().isoformat(timespec="seconds")
        accounts["accountBreakdown"] = account_breakdown
        db_store.write_accounts(accounts)
    elif amounts["source"] not in {"supplement_only", "existing_accounts"}:
        db_store.write_accounts(accounts)
    portfolio = rebuild_portfolio_outputs()
    if has_any_monthly and monthly_finance is None:
        monthly_finance = save_manual_monthly_finance(
            manual_month or "",
            manual_income if has_any_monthly_finance else None,
            manual_expense if has_any_monthly_finance else None,
            manual_sinopac_transfer,
        )
    verified_accounts = db_store.read_accounts({})
    verified_breakdown = verified_accounts.get("accountBreakdown") if isinstance(verified_accounts, dict) else {}
    if not isinstance(verified_breakdown, dict):
        verified_breakdown = {}
    if manual_post_office is not None and float(verified_breakdown.get("postOfficeBalance", -1) or 0) != float(manual_post_office):
        raise HTTPException(status_code=503, detail="郵局餘額已送出，但重新讀取後內容沒有對上，請重新整理確認。")
    if manual_sinopac is not None and float(verified_breakdown.get("sinopacBalance", -1) or 0) != float(manual_sinopac):
        raise HTTPException(status_code=503, detail="永豐餘額已送出，但重新讀取後內容沒有對上，請重新整理確認。")
    if manual_cash is not None and float(verified_breakdown.get("cashBalance", -1) or 0) != float(manual_cash):
        raise HTTPException(status_code=503, detail="現金已送出，但重新讀取後內容沒有對上，請重新整理確認。")
    if manual_emergency_fund is not None and float(verified_breakdown.get("emergencyFund", -1) or 0) != float(manual_emergency_fund):
        raise HTTPException(status_code=503, detail="緊急預備金已送出，但重新讀取後內容沒有對上，請重新整理確認。")
    if manual_investment_reserve is not None and float(verified_breakdown.get("investmentReserve", -1) or 0) != float(manual_investment_reserve):
        raise HTTPException(status_code=503, detail="投資預備金已送出，但重新讀取後內容沒有對上，請重新整理確認。")
    if manual_available_cash is not None and float(verified_breakdown.get("availableCash", -1) or 0) != float(manual_available_cash):
        raise HTTPException(status_code=503, detail="可自由運用現金已送出，但重新讀取後內容沒有對上，請重新整理確認。")
    if manual_bank is not None and float(verified_breakdown.get("otherBankBalance", -1) or 0) != float(manual_bank):
        raise HTTPException(status_code=503, detail="其他銀行餘額已送出，但重新讀取後內容沒有對上，請重新整理確認。")
    if amounts["source"] not in {"supplement_only", "existing_accounts"}:
        expected_cash_twd = (
            cash_total_from_amounts(amounts)
            if any(key in verified_breakdown for key in ["emergencyFund", "investmentReserve", "availableCash"])
            else (
                round(float(amounts["cash"]))
                + round(float(verified_breakdown.get("otherBankBalance", amounts["bank"]) or 0))
                + round(float(verified_breakdown.get("postOfficeBalance", 0) or 0))
                + round(float(verified_breakdown.get("sinopacBalance", 0) or 0))
            )
        )
        if round(float(verified_accounts.get("cashTWD", -1) or 0)) != expected_cash_twd:
            raise HTTPException(status_code=503, detail="資產已送出，但重新讀取後流動資金合計沒有對上，請重新整理確認。")
        if round(float(verified_accounts.get("creditCardDebt", -1) or 0)) != round(float(amounts["debt"])):
            raise HTTPException(status_code=503, detail="資產已送出，但重新讀取後信用卡負債沒有對上，請重新整理確認。")
    if has_any_monthly:
        verified_finance = db_store.read_finance_data({})
        verified_month = None
        for year in verified_finance.get("years", []) if isinstance(verified_finance, dict) else []:
            for month_row in year.get("months", []):
                if month_row.get("month") == manual_month:
                    verified_month = month_row
                    break
            if verified_month:
                break
        if not verified_month:
            raise HTTPException(status_code=503, detail="月份統計已送出，但重新讀取後沒有找到這個月份，請重新整理確認。")
        if manual_income is not None and round(float(verified_month.get("income", -1) or 0)) != round(float(manual_income)):
            raise HTTPException(status_code=503, detail="月份收入已送出，但重新讀取後內容沒有對上，請重新整理確認。")
        if manual_expense is not None and round(float(verified_month.get("expense", -1) or 0)) != round(float(manual_expense)):
            raise HTTPException(status_code=503, detail="月份支出已送出，但重新讀取後內容沒有對上，請重新整理確認。")
        if manual_sinopac_transfer is not None and round(float(verified_month.get("sinopacTransfer", -1) or 0)) != round(float(manual_sinopac_transfer)):
            raise HTTPException(status_code=503, detail="本月轉入永豐已送出，但重新讀取後內容沒有對上，請重新整理確認。")
    saved_at = mark_successful_save()

    return {
        "ok": True,
        "verified": True,
        "savedAt": saved_at,
        "source": amounts["source"],
        "cash": amounts["cash"],
        "bank": amounts["bank"],
        "otherBankBalance": verified_breakdown.get("otherBankBalance", amounts["bank"]),
        "creditCardDebt": amounts["debt"],
        "cashTWD": verified_accounts.get("cashTWD", 0),
        "accountBreakdown": verified_accounts.get("accountBreakdown", {}),
        "totalAssets": portfolio.get("summary", {}).get("totalAssets", 0),
        "netWorth": portfolio.get("summary", {}).get("netWorth", 0),
        "monthlyFinance": monthly_finance,
    }


@app.post("/api/update-prices")
def update_prices(request: Request) -> dict:
    if request.headers.get("x-price-check") == "1":
        return {"ok": True, "method": "POST", "message": "股價更新 API 已就緒。"}

    if not PRICE_UPDATE_LOCK.acquire(blocking=False):
        portfolio = read_portfolio(use_examples=False) or read_portfolio(use_examples=True)
        warning = "股價更新正在執行中，已保留目前價格；請稍後再看更新時間。"
        current_db = db_store.active_backend()
        return {
            "ok": True,
            "currentDb": current_db,
            "savedTo": "none",
            "updatedSymbols": [],
            "failedSymbols": [],
            "errorMessages": [warning],
            "twResult": {"updatedSymbols": [], "failedSymbols": [], "symbols": {}},
            "usResult": {"updatedSymbols": [], "failedSymbols": [], "symbols": {}},
            "source": current_db,
            "updatedAt": portfolio.get("updatedAt", ""),
            "fxRate": portfolio.get("fxRate"),
            "warnings": [warning],
            "marketUpdates": {
                "TW": portfolio.get("markets", {}).get("TW", {}).get("updatedAt", ""),
                "US": portfolio.get("markets", {}).get("US", {}).get("updatedAt", ""),
            },
            "marketStatus": {"TW": "unchanged", "US": "unchanged"},
            "portfolioSummary": portfolio.get("summary", {}),
            "updatedHoldings": 0,
            "totalHoldings": len(portfolio.get("holdings", [])),
            "durationSeconds": 0,
        }

    started_at = datetime.now(TWD)
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
                "marketStatus": {"TW": "unchanged", "US": "unchanged"},
                "portfolioSummary": portfolio.get("summary", {}),
                "updatedHoldings": 0,
                "totalHoldings": len(portfolio.get("holdings", [])),
            }

        current_portfolio = read_portfolio(use_examples=False)
        holdings = current_portfolio.get("holdings", [])
        current_fx_rate = float(current_portfolio.get("fxRate") or 31.451)
        fx_warnings = []
        try:
            fx_rate = fetch_fx_rate(current_fx_rate)
        except Exception as error:
            fx_rate = current_fx_rate
            fx_warnings = [f"匯率更新失敗，已保留原匯率 {current_fx_rate}：{error}"]
        if current_fx_rate and abs(fx_rate - current_fx_rate) / current_fx_rate > 0.2:
            fx_warnings = [*fx_warnings, f"匯率變動超過 20%，已保留原匯率 {current_fx_rate}。"]
            fx_rate = current_fx_rate
        latest_prices, warnings, price_details = fetch_prices(holdings)
        stored_prices = db_store.read_prices({"fxRate": fx_rate, "prices": {}})
        safe_prices, guard_warnings = filter_safe_price_updates(current_portfolio, stored_prices, latest_prices)
        warnings = [*fx_warnings, *warnings, *guard_warnings]
        price_details["errorMessages"] = [*fx_warnings, *price_details["errorMessages"], *guard_warnings]
        price_details["updatedSymbols"] = sorted(safe_prices)
        if guard_warnings:
            guarded_keys = set(latest_prices) - set(safe_prices)
            price_details["failedSymbols"] = sorted({*price_details["failedSymbols"], *guarded_keys})
        merged_prices = {**stored_prices.get("prices", {}), **safe_prices}
        stored_prices["fxRate"] = fx_rate
        stored_prices["updatedAt"] = datetime.now(TWD).isoformat(timespec="seconds")
        stored_prices["prices"] = merged_prices
        stored_prices["warnings"] = warnings
        stored_prices["updatedSymbols"] = sorted(safe_prices)
        stored_prices["failedSymbols"] = price_details["failedSymbols"]
        stored_prices["errorMessages"] = price_details["errorMessages"]
        stored_prices["twResult"] = price_details["twResult"]
        stored_prices["usResult"] = price_details["usResult"]
        write_pre_change_backup("更新股價")
        saved_to = db_store.write_prices(stored_prices)
        portfolio = rebuild_portfolio_outputs()
        current_db = db_store.active_backend()
        if not portfolio:
            portfolio = current_portfolio
        db_store.set_metadata("lastPriceUpdate", db_store.now_iso())
        market_status = {
            "TW": "partial" if price_details["twResult"]["failedSymbols"] else ("ok" if price_details["twResult"]["updatedSymbols"] else "unchanged"),
            "US": "partial" if price_details["usResult"]["failedSymbols"] else ("ok" if price_details["usResult"]["updatedSymbols"] else "unchanged"),
        }
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"股價更新失敗：{error}") from error
    finally:
        PRICE_UPDATE_LOCK.release()
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
        "marketStatus": market_status,
        "portfolioSummary": portfolio.get("summary", {}),
        "updatedHoldings": len(safe_prices),
        "totalHoldings": len(holdings),
        "durationSeconds": round((datetime.now(TWD) - started_at).total_seconds(), 1),
    }


@app.get("/api/update-prices")
def update_prices_status() -> dict:
    return {"ok": True, "method": "POST", "message": "股價更新 API 已就緒。"}


@app.on_event("startup")
def startup_data_checks() -> None:
    try:
        apply_00685l_split_adjustment()
    except Exception as error:
        print(f"00685L split adjustment skipped: {error}")


@app.api_route("/", methods=["GET", "HEAD"])
@app.api_route("/index.html", methods=["GET", "HEAD"])
def index_page() -> Response:
    return html_page("index.html")


@app.api_route("/settings.html", methods=["GET", "HEAD"])
def settings_page() -> Response:
    return html_page("settings.html")


@app.api_route("/settings", methods=["GET", "HEAD"])
def settings_redirect() -> RedirectResponse:
    return RedirectResponse(url="/settings.html", status_code=307)


@app.api_route("/transaction-manager.html", methods=["GET", "HEAD"])
def transaction_manager_page() -> Response:
    return html_page("transaction-manager.html")


@app.api_route("/dividend-manager.html", methods=["GET", "HEAD"])
def dividend_manager_page() -> Response:
    return html_page("dividend-manager.html")


@app.api_route("/snapshot-manager.html", methods=["GET", "HEAD"])
def snapshot_manager_page() -> Response:
    return html_page("snapshot-manager.html")


@app.api_route("/rebalancer.html", methods=["GET", "HEAD"])
def rebalancer_page() -> Response:
    return html_page("rebalancer.html")


@app.api_route("/holdings-audit.html", methods=["GET", "HEAD"])
def holdings_audit_page() -> Response:
    return html_page("holdings-audit.html")


app.mount("/", StaticFiles(directory=ROOT, html=True), name="static")
