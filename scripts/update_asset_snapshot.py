from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from import_moze_csv import import_moze_csv
from portfolio_core import DATA_DIR, build_portfolio, read_json, write_json, write_portfolio_outputs


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="月底匯入 MOZE CSV + 帳戶總覽截圖，更新資產快照。")
    parser.add_argument("--csv", dest="csv_path", help="MOZE CSV 路徑")
    parser.add_argument("--image", dest="image_path", help="MOZE 帳戶總覽截圖路徑")
    parser.add_argument("--cash", type=float, help="手動指定現金總額")
    parser.add_argument("--bank", type=float, help="手動指定銀行總額")
    parser.add_argument("--debt", type=float, help="手動指定信用卡負債總額")
    return parser.parse_args()


def parse_amount(text: str) -> int:
    cleaned = re.sub(r"[^\d.-]", "", text)
    return round(float(cleaned or 0))


def amount_tokens(text: str) -> list[tuple[str, int]]:
    tokens = re.findall(r"[-+]?\s*\$?\s*\d[\d,]*(?:\.\d+)?", text)
    return [(token, parse_amount(token)) for token in tokens]


def ocr_image(image_path: Path) -> str:
    command = ["swift", str(ROOT / "scripts" / "ocr_image.swift"), str(image_path)]
    result = subprocess.run(command, check=True, text=True, capture_output=True)
    return result.stdout


def find_explicit_total(text: str, labels: list[str]) -> int | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    total_words = ["總計", "總額", "總值", "合計"]
    ignored_words = ["可用額度", "額度", "付款日", "到期", "信用額度"]
    for index, line in enumerate(lines):
        if not any(label in line for label in labels):
            continue
        window_lines = []
        for nearby in lines[index:index + 4]:
            if any(word in nearby for word in ignored_words):
                break
            window_lines.append(nearby)
        window = " ".join(window_lines)
        if any(word in window for word in total_words):
            values = amount_tokens(window)
            if values:
                return abs(values[0][1])
    return None


def section_after_label(text: str, labels: list[str], stop_labels: list[str]) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    start = None
    for index, line in enumerate(lines):
        if any(label in line for label in labels):
            start = index
            break
    if start is None:
        return []

    section: list[str] = []
    for line in lines[start + 1:]:
        if any(label in line for label in stop_labels):
            break
        section.append(line)
    return section


def find_category_total(
    text: str,
    labels: list[str],
    stop_labels: list[str],
    require_signed: bool = False,
    allow_section_fallback: bool = True,
) -> int | None:
    explicit = find_explicit_total(text, labels)
    if explicit is not None:
        return explicit
    if not allow_section_fallback:
        return None

    ignored_words = ["可用額度", "信用額度", "額度", "可刷", "付款日"]
    section = [line for line in section_after_label(text, labels, stop_labels) if not any(word in line for word in ignored_words)]

    signed_values: list[int] = []
    all_values: list[int] = []
    for line in section:
        for token, value in amount_tokens(line):
            all_values.append(value)
            if token.strip().startswith(("+", "-")):
                signed_values.append(value)

    if signed_values:
        return abs(signed_values[0])
    if require_signed:
        return None
    return abs(all_values[0]) if all_values else None


def parse_moze_account_ocr(text: str) -> dict[str, int]:
    cash = find_category_total(text, ["現金"], ["銀行", "存款", "信用卡", "卡債", "負債"])
    bank = find_category_total(text, ["銀行", "存款"], ["信用卡", "卡債", "負債"])
    debt = find_category_total(
        text,
        ["信用卡", "卡債"],
        ["負債"],
        require_signed=True,
        allow_section_fallback=False,
    )
    missing = [name for name, value in [("現金", cash), ("銀行", bank), ("信用卡負債", debt)] if value is None]
    if missing:
        raise ValueError(
            "OCR 無法可靠辨識分類總額，請改用手動輸入："
            f"{', '.join(missing)}\n\n辨識文字：\n{text}"
        )
    return {"cash": cash or 0, "bank": bank or 0, "debt": debt or 0}


def update_accounts(cash: int, bank: int, debt: int) -> dict:
    accounts_path = DATA_DIR / "accounts.json"
    accounts = read_json(accounts_path, {})
    breakdown = accounts.get("accountBreakdown") if isinstance(accounts.get("accountBreakdown"), dict) else {}
    breakdown["cashBalance"] = cash
    breakdown["otherBankBalance"] = bank
    accounts["accountBreakdown"] = breakdown
    accounts["cashTWD"] = (
        cash
        + bank
        + round(float(breakdown.get("postOfficeBalance", 0) or 0))
        + round(float(breakdown.get("sinopacBalance", 0) or 0))
    )
    accounts["creditCardDebt"] = debt
    accounts.setdefault("cashUSD", 0)
    accounts.setdefault("otherDebt", 0)
    write_json(accounts_path, accounts)
    return accounts


def main() -> None:
    args = parse_args()
    if args.csv_path:
        import_moze_csv(Path(args.csv_path).expanduser())

    if args.cash is not None and args.bank is not None and args.debt is not None:
        amounts = {"cash": round(args.cash), "bank": round(args.bank), "debt": round(args.debt)}
    elif args.image_path:
        text = ocr_image(Path(args.image_path).expanduser())
        (DATA_DIR / "last-moze-ocr.txt").write_text(text, encoding="utf-8")
        amounts = parse_moze_account_ocr(text)
    else:
        raise SystemExit("請提供 --image，或同時提供 --cash --bank --debt")

    accounts = update_accounts(amounts["cash"], amounts["bank"], amounts["debt"])
    portfolio = build_portfolio()
    write_portfolio_outputs(portfolio)
    print("已更新資產快照")
    print(f"cashTWD: {accounts['cashTWD']:,}")
    print(f"creditCardDebt: {accounts['creditCardDebt']:,}")
    print(f"totalAssets: {portfolio['summary']['totalAssets']:,}")
    print(f"netWorth: {portfolio['summary']['netWorth']:,}")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        print(error.stderr or error.stdout, file=sys.stderr)
        raise
