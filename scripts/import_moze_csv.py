from __future__ import annotations

import argparse
import csv
import json
import shutil
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
MONTHLY_CASHFLOW_PATH = DATA_DIR / "monthly-cashflow.json"
FINANCE_DATA_PATH = ROOT / "finance-data.js"
PORTFOLIO_DATA_PATH = DATA_DIR / "portfolio-data.js"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="匯入 MOZE CSV，更新收入支出摘要與 finance-data.js。")
    parser.add_argument("csv_path", help="MOZE 匯出的 CSV 路徑")
    return parser.parse_args()


def normalize(text: str) -> str:
    return str(text or "").strip().lower().replace(" ", "").replace("_", "")


def choose_field(fields: list[str], candidates: list[str]) -> str | None:
    normalized = {normalize(field): field for field in fields}
    for candidate in candidates:
        key = normalize(candidate)
        if key in normalized:
            return normalized[key]
    for field in fields:
        key = normalize(field)
        if any(normalize(candidate) in key for candidate in candidates):
            return field
    return None


def parse_number(value: str) -> float:
    cleaned = str(value or "").replace(",", "").replace("NT$", "").replace("$", "").strip()
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    return float(cleaned or 0)


def parse_date(value: str) -> str:
    raw = str(value or "").strip()
    formats = [
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y.%m.%d",
        "%m/%d/%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            pass
    if "/" in raw:
        parts = raw.split("/")
        if len(parts) == 3 and len(parts[0]) == 3:
            year = int(parts[0]) + 1911
            return f"{year:04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    raise ValueError(f"無法辨識日期：{raw}")


def load_csv(path: Path) -> list[dict[str, Any]]:
    encodings = ["utf-8-sig", "utf-8", "big5"]
    last_error: Exception | None = None
    for encoding in encodings:
        try:
            with path.open("r", encoding=encoding, newline="") as file:
                return list(csv.DictReader(file))
        except UnicodeDecodeError as error:
            last_error = error
    raise RuntimeError(f"CSV 編碼讀取失敗：{last_error}")


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_finance_data() -> dict[str, Any] | None:
    if not FINANCE_DATA_PATH.exists():
        return None
    raw = FINANCE_DATA_PATH.read_text(encoding="utf-8").strip()
    prefix = "window.financeData = "
    if raw.startswith(prefix):
        raw = raw[len(prefix):]
    if raw.endswith(";"):
        raw = raw[:-1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def backup_existing_files() -> None:
    backup_dir = DATA_DIR / "backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
    files = [
        FINANCE_DATA_PATH,
        MONTHLY_CASHFLOW_PATH,
        PORTFOLIO_DATA_PATH,
    ]
    for path in files:
        if path.exists():
            backup_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup_dir / path.name)


def seed_monthly_cashflow() -> dict[str, Any]:
    if MONTHLY_CASHFLOW_PATH.exists():
        return read_json(MONTHLY_CASHFLOW_PATH, {"months": {}})

    finance_data = read_finance_data()
    months: dict[str, dict[str, Any]] = {}
    transactions_by_month: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if finance_data:
        for transaction in finance_data.get("transactions", []):
            month_key = transaction.get("month") or str(transaction.get("date", ""))[:7]
            if month_key:
                transactions_by_month[month_key].append(transaction)
        for year in finance_data.get("years", []):
            for month in year.get("months", []):
                month_key = month["month"]
                months[month_key] = {
                    **month,
                    "source": finance_data.get("source", "finance-data.js"),
                    "updatedAt": finance_data.get("generatedAt", ""),
                    "transactionsData": transactions_by_month.get(month_key, []),
                }
    return {"schemaVersion": 1, "months": months}


def classify(row: dict[str, Any], type_field: str | None) -> str | None:
    if not type_field:
        raise ValueError("找不到記錄類型欄位，無法正確統計收入支出。")
    label = str(row.get(type_field, "")).strip()
    if label == "收入":
        return "收入"
    if label == "支出":
        return "支出"
    return None


def import_moze_csv(csv_path: Path) -> dict[str, Any]:
    backup_existing_files()
    rows = load_csv(csv_path)
    if not rows:
        raise ValueError("CSV 沒有資料")

    fields = list(rows[0].keys())
    date_field = choose_field(fields, ["日期", "date", "交易日期", "時間"])
    amount_field = choose_field(fields, ["金額", "amount", "支出", "收入"])
    type_field = choose_field(fields, ["記錄類型", "類型", "type", "收支", "交易類型"])
    account_field = choose_field(fields, ["帳戶", "account", "錢包"])
    category_field = choose_field(fields, ["分類", "主分類", "category"])
    subcategory_field = choose_field(fields, ["子分類", "subcategory"])
    name_field = choose_field(fields, ["名稱", "name", "備註", "描述", "description"])

    if not date_field or not amount_field or not type_field:
        raise ValueError(f"找不到日期、金額或記錄類型欄位。CSV 欄位：{fields}")

    transactions = []
    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "income": 0,
        "expense": 0,
        "net": 0,
        "transactions": 0,
    })

    for index, row in enumerate(rows, start=1):
        try:
            tx_date = parse_date(row.get(date_field, ""))
            amount = parse_number(row.get(amount_field, "0"))
        except ValueError:
            continue

        tx_type = classify(row, type_field)
        if tx_type is None:
            continue
        month = tx_date[:7]
        year = tx_date[:4]
        signed_amount = amount if tx_type == "收入" else -abs(amount)
        if tx_type == "收入":
            grouped[month]["income"] += abs(amount)
        else:
            grouped[month]["expense"] += abs(amount)
        grouped[month]["net"] += signed_amount
        grouped[month]["transactions"] += 1

        transactions.append({
            "id": index,
            "date": tx_date,
            "account": row.get(account_field, "") if account_field else "",
            "type": tx_type,
            "main": row.get(category_field, tx_type) if category_field else tx_type,
            "sub": row.get(subcategory_field, "") if subcategory_field else "",
            "amount": signed_amount,
            "name": row.get(name_field, "") if name_field else "",
            "year": year,
            "month": month,
        })

    imported_months = []
    for month, item in sorted(grouped.items()):
        income = round(item["income"])
        expense = round(item["expense"])
        net = round(item["net"])
        savings_rate = round((net / income * 100), 1) if income else 0
        month_transactions = [transaction for transaction in transactions if transaction["month"] == month]
        imported_months.append({
            "month": month,
            "income": income,
            "expense": expense,
            "net": net,
            "savingsRate": savings_rate,
            "transactions": item["transactions"],
            "source": csv_path.name,
            "updatedAt": datetime.now().isoformat(timespec="seconds"),
            "transactionsData": month_transactions,
        })

    monthly_cashflow = seed_monthly_cashflow()
    monthly_cashflow.setdefault("schemaVersion", 1)
    monthly_cashflow.setdefault("months", {})
    for month in imported_months:
        monthly_cashflow["months"][month["month"]] = month
    write_json(MONTHLY_CASHFLOW_PATH, monthly_cashflow)

    months = sorted(
        monthly_cashflow["months"].values(),
        key=lambda item: item["month"],
    )

    years = []
    year_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for month in months:
        year_map[month["month"][:4]].append(month)
    for year, year_months in sorted(year_map.items(), reverse=True):
        income = sum(month["income"] for month in year_months)
        expense = sum(month["expense"] for month in year_months)
        net = sum(month["net"] for month in year_months)
        years.append({
            "year": year,
            "income": income,
            "expense": expense,
            "net": net,
            "savingsRate": round((net / income * 100), 1) if income else 0,
            "transactions": sum(month["transactions"] for month in year_months),
            "months": [
                {key: value for key, value in month.items() if key != "transactionsData"}
                for month in sorted(year_months, key=lambda item: item["month"], reverse=True)
            ],
        })

    merged_transactions = []
    for month in months:
        merged_transactions.extend(month.get("transactionsData", []))

    payload = {
        "source": csv_path.name,
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "recordCount": len(merged_transactions),
        "years": years,
        "transactions": sorted(merged_transactions, key=lambda item: (item.get("date", ""), item.get("id", 0))),
        "importedMonths": [month["month"] for month in imported_months],
    }
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "moze-finance-data.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    FINANCE_DATA_PATH.write_text(
        "window.financeData = " + json.dumps(payload, ensure_ascii=False) + ";\n",
        encoding="utf-8",
    )
    return payload


if __name__ == "__main__":
    result = import_moze_csv(Path(parse_args().csv_path).expanduser())
    print(f"已匯入 {result['recordCount']} 筆 MOZE CSV")
