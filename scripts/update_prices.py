from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.portfolio_core import write_portfolio_outputs


ROOT = Path(__file__).resolve().parents[1]
PORTFOLIO_PATH = ROOT / "data" / "portfolio.json"
TWD = timezone(timedelta(hours=8))
DEFAULT_FX_RATE = 31.451


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_json(url: str) -> Any:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_number(value: Any, default: float = 0.0) -> float:
    try:
        text = str(value).strip().replace(",", "").replace("+", "")
        if text in {"", "-", "--", "X"}:
            return default
        return float(text.replace("X", ""))
    except (TypeError, ValueError):
        return default


def now_text() -> str:
    return datetime.now(TWD).strftime("%Y/%m/%d %H:%M")


def now_iso() -> str:
    return datetime.now(TWD).isoformat(timespec="seconds")


def yahoo_chart(symbol: str) -> dict[str, Any] | None:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol)}?range=5d&interval=1d"
    payload = fetch_json(url)
    result = payload.get("chart", {}).get("result", [None])[0]
    if not result:
        return None

    meta = result.get("meta", {})
    price = parse_number(meta.get("regularMarketPrice"))
    if price <= 0:
        return None

    previous = parse_number(meta.get("previousClose"), price)
    change = price - previous
    timestamp = meta.get("regularMarketTime")
    updated_at = datetime.fromtimestamp(timestamp, TWD) if timestamp else datetime.now(TWD)

    return {
        "price": price,
        "change": round(change, 4),
        "changePercent": round((change / previous * 100) if previous else 0, 2),
        "updatedAt": updated_at.strftime("%Y/%m/%d %H:%M"),
    }


def price_result(ok: bool, symbol: str, market: str, row: dict[str, Any] | None = None, error: str = "", source: str = "") -> dict[str, Any]:
    return {
        "ok": ok,
        "symbol": f"{market}:{symbol}",
        "source": source,
        "price": (row or {}).get("price"),
        "updatedAt": (row or {}).get("updatedAt", ""),
        "error": error,
    }


def fetch_tw_prices(symbols: set[str]) -> dict[str, dict[str, Any]]:
    if not symbols:
        return {}

    prices: dict[str, dict[str, Any]] = {}
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    rows = fetch_json(url)
    rows_by_code = {str(row.get("Code")): row for row in rows if row.get("Code")}

    for symbol in symbols:
        row = rows_by_code.get(symbol)
        if not row:
            continue

        price = parse_number(row.get("ClosingPrice"))
        if price <= 0:
            continue

        change = parse_number(row.get("Change"))
        previous = price - change
        prices[symbol] = {
            "price": price,
            "change": round(change, 4),
            "changePercent": round((change / previous * 100) if previous else 0, 2),
            "updatedAt": now_text(),
        }

    return prices


def fetch_tw_price_fallback(symbol: str) -> dict[str, Any] | None:
    today = datetime.now(TWD).strftime("%Y%m%d")
    url = (
        "https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY"
        f"?date={today}&stockNo={quote(symbol)}&response=json"
    )
    payload = fetch_json(url)
    rows = payload.get("data") or []
    if not rows:
        return yahoo_chart(f"{symbol}.TW")

    latest = rows[-1]
    price = parse_number(latest[6])
    if price <= 0:
        return yahoo_chart(f"{symbol}.TW")

    change = parse_number(latest[7])
    roc_year, month, day = str(latest[0]).split("/")
    updated_at = f"{int(roc_year) + 1911}/{month.zfill(2)}/{day.zfill(2)} {datetime.now(TWD).strftime('%H:%M')}"
    previous = price - change

    return {
        "price": price,
        "change": round(change, 4),
        "changePercent": round((change / previous * 100) if previous else 0, 2),
        "updatedAt": updated_at,
    }


def fetch_prices(holdings: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[str], dict[str, Any]]:
    warnings: list[str] = []
    error_messages: list[str] = []
    prices: dict[str, dict[str, Any]] = {}
    tw_result: dict[str, Any] = {
        "batchOk": True,
        "batchError": "",
        "updatedSymbols": [],
        "failedSymbols": [],
        "symbols": {},
    }
    us_result: dict[str, Any] = {
        "updatedSymbols": [],
        "failedSymbols": [],
        "symbols": {},
    }
    tw_symbols = {
        str(holding.get("symbol", "")).upper()
        for holding in holdings
        if str(holding.get("market", "")).upper() == "TW" and holding.get("symbol")
    }

    try:
        for symbol, row in fetch_tw_prices(tw_symbols).items():
            key = f"TW:{symbol}"
            prices[key] = row
            tw_result["updatedSymbols"].append(key)
            tw_result["symbols"][key] = price_result(True, symbol, "TW", row, source="twse-batch")
    except Exception as error:
        message = f"台股批次更新失敗，改用逐檔更新：{type(error).__name__}: {error}"
        warnings.append(message)
        error_messages.append(message)
        tw_result["batchOk"] = False
        tw_result["batchError"] = str(error)

    for symbol in sorted(tw_symbols):
        key = f"TW:{symbol}"
        if key in prices:
            continue
        try:
            row = fetch_tw_price_fallback(symbol)
            if row:
                prices[key] = row
                tw_result["updatedSymbols"].append(key)
                tw_result["symbols"][key] = price_result(True, symbol, "TW", row, source="twse-symbol-or-yahoo")
            else:
                message = f"{key} 更新失敗，資料來源沒有回傳可用價格"
                warnings.append(message)
                error_messages.append(message)
                tw_result["failedSymbols"].append(key)
                tw_result["symbols"][key] = price_result(False, symbol, "TW", error=message, source="twse-symbol-or-yahoo")
            time.sleep(0.2)
        except (URLError, TimeoutError, ValueError, KeyError, TypeError) as error:
            message = f"{key} 更新失敗，保留原價格：{type(error).__name__}: {error}"
            warnings.append(message)
            error_messages.append(message)
            tw_result["failedSymbols"].append(key)
            tw_result["symbols"][key] = price_result(False, symbol, "TW", error=message, source="twse-symbol-or-yahoo")

    for holding in holdings:
        market = str(holding.get("market", "")).upper()
        symbol = str(holding.get("symbol", "")).upper()
        if market != "US" or not symbol:
            continue

        try:
            row = yahoo_chart(symbol)
            if row:
                key = f"US:{symbol}"
                prices[key] = row
                us_result["updatedSymbols"].append(key)
                us_result["symbols"][key] = price_result(True, symbol, "US", row, source="yahoo")
            else:
                key = f"US:{symbol}"
                message = f"{key} 更新失敗，Yahoo 沒有回傳可用價格"
                warnings.append(message)
                error_messages.append(message)
                us_result["failedSymbols"].append(key)
                us_result["symbols"][key] = price_result(False, symbol, "US", error=message, source="yahoo")
            time.sleep(0.2)
        except (URLError, TimeoutError, ValueError, KeyError, TypeError) as error:
            key = f"US:{symbol}"
            message = f"{key} 更新失敗，保留原價格：{type(error).__name__}: {error}"
            warnings.append(message)
            error_messages.append(message)
            us_result["failedSymbols"].append(key)
            us_result["symbols"][key] = price_result(False, symbol, "US", error=message, source="yahoo")

    details = {
        "updatedSymbols": sorted(prices.keys()),
        "failedSymbols": sorted(set(tw_result["failedSymbols"] + us_result["failedSymbols"])),
        "errorMessages": error_messages,
        "twResult": tw_result,
        "usResult": us_result,
    }
    return prices, warnings, details


def fetch_fx_rate(current_fx_rate: float) -> float:
    try:
        row = yahoo_chart("USDTWD=X")
        if row and row["price"] > 0:
            return round(float(row["price"]), 4)
    except Exception:
        pass
    return current_fx_rate


def enrich_holding(holding: dict[str, Any], price_row: dict[str, Any] | None, fx_rate: float) -> dict[str, Any]:
    updated = dict(holding)
    market = str(updated.get("market", "")).upper()
    shares = parse_number(updated.get("shares"))
    total_cost = parse_number(updated.get("totalCost"))
    price = parse_number((price_row or {}).get("price"), parse_number(updated.get("price")))
    multiplier = fx_rate if market == "US" else 1

    market_value = shares * price
    unrealized_gain = market_value - total_cost
    return_rate = (unrealized_gain / total_cost * 100) if total_cost else 0

    updated["market"] = market
    updated["price"] = round(price, 4)
    updated["marketValue"] = round(market_value, 2)
    updated["unrealizedGain"] = round(unrealized_gain, 2)
    updated["returnRate"] = round(return_rate, 2)
    updated["marketValueTWD"] = round(market_value * multiplier)
    updated["totalCostTWD"] = round(total_cost * multiplier)
    updated["unrealizedGainTWD"] = round(unrealized_gain * multiplier)

    if shares:
        updated["averageCost"] = round(total_cost / shares, 4)

    if price_row:
        updated["change"] = price_row.get("change", 0)
        updated["changePercent"] = price_row.get("changePercent", 0)
        updated["updatedAt"] = price_row.get("updatedAt", now_text())

    return updated


def recalculate_portfolio(portfolio: dict[str, Any], prices: dict[str, dict[str, Any]], fx_rate: float) -> dict[str, Any]:
    holdings = []
    for holding in portfolio.get("holdings", []):
        market = str(holding.get("market", "")).upper()
        symbol = str(holding.get("symbol", "")).upper()
        holdings.append(enrich_holding(holding, prices.get(f"{market}:{symbol}"), fx_rate))

    accounts = portfolio.get("accounts", {})
    tw_holdings = [row for row in holdings if row.get("market") == "TW"]
    us_holdings = [row for row in holdings if row.get("market") == "US"]

    tw_market_value = round(sum(parse_number(row.get("marketValueTWD")) for row in tw_holdings))
    us_market_value = round(sum(parse_number(row.get("marketValueTWD")) for row in us_holdings))
    tw_cost = round(sum(parse_number(row.get("totalCostTWD")) for row in tw_holdings))
    us_cost = round(sum(parse_number(row.get("totalCostTWD")) for row in us_holdings))
    cash_twd = round(parse_number(accounts.get("cashTWD")) + parse_number(accounts.get("cashUSD")) * fx_rate)
    debt = round(parse_number(accounts.get("creditCardDebt")) + parse_number(accounts.get("otherDebt")))
    stock_assets = tw_market_value + us_market_value
    total_assets = stock_assets + cash_twd

    def gain(rows: list[dict[str, Any]]) -> int:
        return round(sum(parse_number(row.get("unrealizedGainTWD")) for row in rows))

    def rate(market_value: int, cost: int) -> float:
        return round(((market_value - cost) / cost * 100), 2) if cost else 0

    portfolio["updatedAt"] = now_iso()
    portfolio["fxRate"] = fx_rate
    portfolio["holdings"] = holdings
    portfolio["summary"] = {
        "totalAssets": total_assets,
        "stockAssets": stock_assets,
        "cash": cash_twd,
        "debt": debt,
        "netWorth": total_assets - debt,
    }
    portfolio["markets"] = {
        "TW": {
            "marketValue": tw_market_value,
            "cost": tw_cost,
            "unrealizedGain": gain(tw_holdings),
            "returnRate": rate(tw_market_value, tw_cost),
            "updatedAt": max((str(row.get("updatedAt", "")) for row in tw_holdings), default=""),
        },
        "US": {
            "marketValueUSD": round(sum(parse_number(row.get("marketValue")) for row in us_holdings), 2),
            "marketValue": us_market_value,
            "costUSD": round(sum(parse_number(row.get("totalCost")) for row in us_holdings), 2),
            "cost": us_cost,
            "unrealizedGainUSD": round(sum(parse_number(row.get("unrealizedGain")) for row in us_holdings), 2),
            "unrealizedGain": gain(us_holdings),
            "returnRate": rate(us_market_value, us_cost),
            "cashUSD": parse_number(accounts.get("cashUSD")),
            "updatedAt": max((str(row.get("updatedAt", "")) for row in us_holdings), default=""),
        },
    }
    portfolio["allocation"] = {
        "taiwanStocks": tw_market_value,
        "usStocks": us_market_value,
        "cash": cash_twd,
        "debt": debt,
    }

    return portfolio


def update_prices() -> dict[str, Any]:
    portfolio = read_json(PORTFOLIO_PATH)
    holdings = portfolio.get("holdings", [])
    fx_rate = fetch_fx_rate(parse_number(portfolio.get("fxRate"), DEFAULT_FX_RATE))
    prices, warnings, details = fetch_prices(holdings)
    updated = recalculate_portfolio(portfolio, prices, fx_rate)
    write_portfolio_outputs(updated)

    return {
        "updatedHoldings": len(prices),
        "totalHoldings": len(holdings),
        "warnings": warnings,
        **details,
        "portfolio": updated,
    }


if __name__ == "__main__":
    result = update_prices()
    print(f"已更新 portfolio.json：{result['updatedHoldings']}/{result['totalHoldings']} 檔取得最新價格")
    for warning in result["warnings"]:
        print(f"WARNING: {warning}")
