from __future__ import annotations

import argparse
import json
from pathlib import Path

from portfolio_core import DATA_DIR, build_portfolio, read_json, write_json, write_portfolio_outputs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="新增買進或賣出交易，並重算持股。")
    parser.add_argument("--date", required=True, help="交易日期，例如 2026-06-01")
    parser.add_argument("--symbol", required=True, help="商品代號，例如 0050 或 VOO")
    parser.add_argument("--name", default="", help="商品名稱，例如 元大台灣50")
    parser.add_argument("--market", required=True, choices=["TW", "US"], help="市場")
    parser.add_argument("--action", required=True, choices=["BUY", "SELL"], help="買進或賣出")
    parser.add_argument("--shares", required=True, type=float, help="股數")
    parser.add_argument("--price", required=True, type=float, help="成交價格")
    parser.add_argument("--fee", default=0, type=float, help="手續費")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    path = DATA_DIR / "transactions.json"
    transactions = read_json(path, [])
    transaction = {
        "date": args.date,
        "symbol": args.symbol.upper(),
        "name": args.name or args.symbol.upper(),
        "market": args.market,
        "action": args.action,
        "shares": args.shares,
        "price": args.price,
        "fee": args.fee,
    }
    transactions.append(transaction)
    write_json(path, transactions)
    portfolio = build_portfolio()
    write_portfolio_outputs(portfolio)
    print("已新增交易：")
    print(json.dumps(transaction, ensure_ascii=False, indent=2))
    print(f"已重算淨資產：{portfolio['summary']['netWorth']:,}")


if __name__ == "__main__":
    main()
