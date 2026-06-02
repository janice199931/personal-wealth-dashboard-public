from portfolio_core import build_portfolio, write_portfolio_outputs


if __name__ == "__main__":
    portfolio = build_portfolio()
    write_portfolio_outputs(portfolio)
    print("Portfolio rebuilt")
    print(f"Total assets: {portfolio['summary']['totalAssets']:,}")
    print(f"Net worth: {portfolio['summary']['netWorth']:,}")
