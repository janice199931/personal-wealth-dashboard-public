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
def db_status() -> dict[str, Any]:
    return db_store.status()


@app.get("/api/portfolio")
def get_portfolio() -> dict[str, Any]:
    portfolio = read_portfolio(use_examples=True)
    return {"ok": True, "portfolio": portfolio, "source": "sqlite" if db_store.read_latest_portfolio({}) else "example"}


@app.get("/api/net-worth-history")
def get_net_worth_history() -> dict[str, Any]:
    history = read_net_worth_history(use_examples=True)
    return {"ok": True, "history": history, "source": "sqlite" if db_store.read_net_worth_history() else "example"}


@app.post("/api/db/import-json")
def import_json_to_db() -> dict[str, Any]:
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

    portfolio = rebuild_portfolio_outputs()
    return {
        "ok": True,
        "imported": imported,
        "counts": db_store.status()["counts"],
        "portfolioSummary": portfolio.get("summary", {}),
        "message": "JSON 匯入 SQLite 完成。正式 JSON 仍應留在本機並由 .gitignore 忽略。",
    }


@app.post("/api/db/rebuild-portfolio")
def rebuild_portfolio_from_db() -> dict[str, Any]:
    portfolio = rebuild_portfolio_outputs()
    return {
        "ok": True,
        "counts": db_store.status()["counts"],
        "portfolioSummary": portfolio.get("summary", {}),
    }


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
    return {"ok": True, "transactions": transactions}


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
    dividends = read_dividends(use_examples=True)
    dividends, changed = ensure_dividend_ids(dividends)
    if changed:
        write_dividends(dividends)
    return {"ok": True, "dividends": dividends}


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
        current_portfolio = read_portfolio(use_examples=True)
        holdings = current_portfolio.get("holdings", [])
        fx_rate = fetch_fx_rate(float(current_portfolio.get("fxRate") or 31.451))
        latest_prices, warnings = fetch_prices(holdings)
        has_formal_data = bool(
            db_store.read_transactions()
            or db_store.read_accounts({})
            or db_store.read_latest_portfolio({})
        )
        if has_formal_data:
            stored_prices = db_store.read_prices({"fxRate": fx_rate, "prices": {}})
            stored_prices["fxRate"] = fx_rate
            stored_prices["prices"] = {**stored_prices.get("prices", {}), **latest_prices}
            db_store.write_prices(stored_prices)
            portfolio = rebuild_portfolio_outputs()
        else:
            portfolio = current_portfolio
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"股價更新失敗：{error}") from error
    return {
        "ok": True,
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


app.mount("/", StaticFiles(directory=ROOT, html=True), name="static")
