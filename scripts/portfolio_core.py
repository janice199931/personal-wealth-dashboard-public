from __future__ import annotations

import json
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
TWD = timezone(timedelta(hours=8))


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def today_iso() -> str:
    return datetime.now(TWD).date().isoformat()


def now_iso() -> str:
    return datetime.now(TWD).isoformat(timespec="seconds")


def normalize_history_date(value: Any) -> str:
    text = str(value or "").strip().replace("/", "-")
    if "T" in text:
        text = text.split("T", 1)[0]
    else:
        text = text[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return ""


def position_key(transaction: dict[str, Any]) -> str:
    return f"{transaction['market'].upper()}:{transaction['symbol'].upper()}"


def load_inputs() -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, Any]]:
    transactions = read_json(DATA_DIR / "transactions.json", [])
    accounts = read_json(DATA_DIR / "accounts.json", {})
    prices = read_json(DATA_DIR / "prices.json", {"fxRate": 31.451, "prices": {}})
    current_portfolio = read_json(DATA_DIR / "portfolio.json", {})
    return transactions, accounts, prices, current_portfolio


def current_portfolio_price_book(portfolio: dict[str, Any]) -> dict[str, dict[str, Any]]:
    price_book: dict[str, dict[str, Any]] = {}
    for holding in portfolio.get("holdings", []):
        market = str(holding.get("market", "")).upper()
        symbol = str(holding.get("symbol", "")).upper()
        if not market or not symbol:
            continue
        price_book[f"{market}:{symbol}"] = {
            "price": holding.get("price", 0),
            "change": holding.get("change", 0),
            "changePercent": holding.get("changePercent", 0),
            "updatedAt": holding.get("updatedAt", ""),
        }
    return price_book


def calculate_positions(transactions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    positions: dict[str, dict[str, Any]] = {}

    for item in sorted(transactions, key=lambda row: row.get("date", "")):
        market = item["market"].upper()
        symbol = item["symbol"].upper()
        key = f"{market}:{symbol}"
        action = item["action"].upper()
        shares = float(item["shares"])
        price = float(item["price"])
        fee = float(item.get("fee", 0))

        position = positions.setdefault(
            key,
            {
                "symbol": symbol,
                "name": item.get("name", symbol),
                "market": market,
                "shares": 0.0,
                "cost": 0.0,
                "realizedGain": 0.0,
            },
        )

        if action == "BUY":
            position["shares"] += shares
            position["cost"] += shares * price + fee
        elif action == "SELL":
            if shares > position["shares"]:
                raise ValueError(f"{key} 賣出股數大於目前持股")
            avg_cost = position["cost"] / position["shares"] if position["shares"] else 0
            sold_cost = avg_cost * shares
            proceeds = shares * price - fee
            position["shares"] -= shares
            position["cost"] -= sold_cost
            position["realizedGain"] += proceeds - sold_cost
        else:
            raise ValueError(f"不支援的交易動作：{action}")

    return {
        key: value
        for key, value in positions.items()
        if value["shares"] > 0.000001
    }


def enrich_position(position: dict[str, Any], price_book: dict[str, Any], fx_rate: float) -> dict[str, Any]:
    key = f"{position['market']}:{position['symbol']}"
    price_row = price_book.get(key, {})
    latest_price = float(price_row.get("price", position["cost"] / position["shares"]))
    shares = float(position["shares"])
    cost = float(position["cost"])
    market_value_native = shares * latest_price
    gain_native = market_value_native - cost
    return_rate = (gain_native / cost * 100) if cost else 0
    multiplier = fx_rate if position["market"] == "US" else 1

    return {
        "symbol": position["symbol"],
        "name": position["name"],
        "market": position["market"],
        "shares": round(shares, 6),
        "price": round(latest_price, 4),
        "averageCost": round(cost / shares, 4) if shares else 0,
        "totalCost": round(cost, 2),
        "realizedGain": round(float(position.get("realizedGain", 0)), 2),
        "marketValue": round(market_value_native, 2),
        "unrealizedGain": round(gain_native, 2),
        "returnRate": round(return_rate, 2),
        "change": price_row.get("change", 0),
        "changePercent": price_row.get("changePercent", 0),
        "updatedAt": price_row.get("updatedAt", ""),
        "marketValueTWD": round(market_value_native * multiplier),
        "totalCostTWD": round(cost * multiplier),
        "unrealizedGainTWD": round(gain_native * multiplier),
    }


def build_portfolio_from_data(
    transactions: list[dict[str, Any]],
    accounts: dict[str, Any],
    prices: dict[str, Any],
    current_portfolio: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current_portfolio = current_portfolio or {}
    fx_rate = float(current_portfolio.get("fxRate") or prices.get("fxRate", 31.451))
    price_book = {
        **current_portfolio_price_book(current_portfolio),
        **prices.get("prices", {}),
    }
    positions = calculate_positions(transactions)
    holdings = [enrich_position(position, price_book, fx_rate) for position in positions.values()]
    tw_holdings = [row for row in holdings if row["market"] == "TW"]
    us_holdings = [row for row in holdings if row["market"] == "US"]

    tw_market_value = round(sum(row["marketValueTWD"] for row in tw_holdings))
    us_market_value = round(sum(row["marketValueTWD"] for row in us_holdings))
    tw_cost = round(sum(row["totalCostTWD"] for row in tw_holdings))
    us_cost = round(sum(row["totalCostTWD"] for row in us_holdings))
    cash_twd = round(float(accounts.get("cashTWD", 0)) + float(accounts.get("cashUSD", 0)) * fx_rate)
    debt = round(float(accounts.get("creditCardDebt", 0)) + float(accounts.get("otherDebt", 0)))
    stock_assets = tw_market_value + us_market_value
    total_assets = stock_assets + cash_twd
    net_worth = total_assets - debt

    def gain(rows: list[dict[str, Any]]) -> int:
        return round(sum(row["unrealizedGainTWD"] for row in rows))

    def rate(market_value: int, cost: int) -> float:
        return round(((market_value - cost) / cost * 100), 2) if cost else 0

    latest_tw = max((row.get("updatedAt", "") for row in tw_holdings), default="")
    latest_us = max((row.get("updatedAt", "") for row in us_holdings), default="")

    return {
        "schemaVersion": 1,
        "updatedAt": now_iso(),
        "fxRate": fx_rate,
        "accounts": accounts,
        "summary": {
            "totalAssets": total_assets,
            "stockAssets": stock_assets,
            "cash": cash_twd,
            "debt": debt,
            "netWorth": net_worth,
        },
        "markets": {
            "TW": {
                "marketValue": tw_market_value,
                "cost": tw_cost,
                "unrealizedGain": gain(tw_holdings),
                "returnRate": rate(tw_market_value, tw_cost),
                "updatedAt": latest_tw,
            },
            "US": {
                "marketValueUSD": round(sum(row["marketValue"] for row in us_holdings), 2),
                "marketValue": us_market_value,
                "costUSD": round(sum(row["totalCost"] for row in us_holdings), 2),
                "cost": us_cost,
                "unrealizedGainUSD": round(sum(row["unrealizedGain"] for row in us_holdings), 2),
                "unrealizedGain": gain(us_holdings),
                "returnRate": rate(us_market_value, us_cost),
                "cashUSD": float(accounts.get("cashUSD", 0)),
                "updatedAt": latest_us,
            },
        },
        "allocation": {
            "taiwanStocks": tw_market_value,
            "usStocks": us_market_value,
            "cash": cash_twd,
            "debt": debt,
        },
        "holdings": holdings,
    }


def build_portfolio() -> dict[str, Any]:
    transactions, accounts, prices, current_portfolio = load_inputs()
    return build_portfolio_from_data(transactions, accounts, prices, current_portfolio)


def update_history_from_data(portfolio: dict[str, Any], history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    current = {"date": today_iso(), "netWorth": portfolio["summary"]["netWorth"]}

    by_date: dict[str, dict[str, Any]] = {}
    for row in history:
        normalized_date = normalize_history_date(row.get("date"))
        if not normalized_date:
            continue
        try:
            net_worth = round(float(row.get("netWorth", 0)))
        except (TypeError, ValueError):
            continue
        by_date[normalized_date] = {
            "date": normalized_date,
            "netWorth": net_worth,
        }

    by_date[current["date"]] = current
    return sorted(by_date.values(), key=lambda row: row["date"])


def update_history(portfolio: dict[str, Any]) -> list[dict[str, Any]]:
    history_path = DATA_DIR / "net-worth-history.json"
    history = read_json(history_path, [])
    normalized_history = update_history_from_data(portfolio, history)
    write_json(history_path, normalized_history)
    return normalized_history


def write_portfolio_outputs(portfolio: dict[str, Any], update_net_worth_history: bool = True) -> None:
    write_json(DATA_DIR / "portfolio.json", portfolio)
    if update_net_worth_history:
        history = update_history(portfolio)
    else:
        history = read_json(DATA_DIR / "net-worth-history.json", [])
    js = (
        "window.portfolioData = "
        + json.dumps(portfolio, ensure_ascii=False, indent=2)
        + ";\nwindow.netWorthHistory = "
        + json.dumps(history, ensure_ascii=False, indent=2)
        + ";\n"
    )
    (DATA_DIR / "portfolio-data.js").write_text(js, encoding="utf-8")


if __name__ == "__main__":
    result = build_portfolio()
    write_portfolio_outputs(result)
    print(f"已更新 portfolio.json，淨資產 {result['summary']['netWorth']:,}")
