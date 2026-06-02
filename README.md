# Personal Wealth Dashboard Public Edition

這是可上傳 GitHub 的乾淨部署版本。此資料夾不包含正式資產資料、備份資料、`.env` 或私人 `finance-data.js`。

## Included Files

- `index.html`
- `styles.css`
- `app.js`
- `server.py`
- `requirements.txt`
- `scripts/`
- `data/example-*.json`

## Private Data Rules

不要提交自己的正式資料：

- `data/*.json`
- `data/*.txt`
- `data/*.js`
- `data/backups/`
- `data/test-backups/`
- `.env`
- `finance-data.js`

`.gitignore` 已經保護上述路徑。範例檔 `data/example-*.json` 會保留在 Git 內，方便看格式。

## Login Password

網站使用瀏覽器原生登入視窗保護所有頁面與 API。

必填環境變數：

```bash
ASSET_DASHBOARD_PASSWORD="replace-with-a-strong-password"
```

可選環境變數：

```bash
ASSET_DASHBOARD_USERNAME="admin"
```

如果沒有設定 `ASSET_DASHBOARD_PASSWORD`，網站會拒絕登入，避免公開部署後裸露資料。

## Run Locally

```bash
pip install -r requirements.txt
ASSET_DASHBOARD_PASSWORD="local-password" \
python -m uvicorn server:app --reload --host 127.0.0.1 --port 8000
```

打開：

```text
http://127.0.0.1:8000/
```

## Deploy to Render / Railway

Build command：

```bash
pip install -r requirements.txt
```

Start command：

```bash
uvicorn server:app --host 0.0.0.0 --port $PORT
```

Environment variables：

```text
ASSET_DASHBOARD_PASSWORD=replace-with-a-strong-password
ASSET_DASHBOARD_USERNAME=admin
```

## Import Your Own Data

Public Edition 內建 Demo Mode。當正式資料不存在時，首頁和讀取 API 會自動使用範例資料：

- `data/example-portfolio.json`
- `data/example-transactions.json`
- `data/example-dividends.json`

當正式資料存在時，系統會優先使用正式資料：

- `data/portfolio.json`
- `data/transactions.json`
- `data/dividends.json`

範例檔放在 `data/`：

- `data/example-portfolio.json`
- `data/example-transactions.json`
- `data/example-dividends.json`
- `data/example-accounts.json`
- `data/example-prices.json`
- `data/example-net-worth-history.json`

使用自己的資料時，請在部署環境或本機建立正式檔名：

```bash
cp data/example-transactions.json data/transactions.json
cp data/example-dividends.json data/dividends.json
cp data/example-accounts.json data/accounts.json
cp data/example-prices.json data/prices.json
cp data/example-portfolio.json data/portfolio.json
cp data/example-net-worth-history.json data/net-worth-history.json
```

然後把內容換成自己的資料。

重新產生首頁資料：

```bash
python scripts/rebuild_portfolio.py
```

注意：Render / Railway 的一般檔案系統不適合長期保存正式資產資料。如果部署後會新增交易、更新股價或匯入資料，請另外規劃資料庫、持久化磁碟或只在本機維護資料後重新部署。

## Safety Checklist Before GitHub Push

```bash
git status --short
git check-ignore -v data/transactions.json data/portfolio.json data/backups/example.json .env finance-data.js
```

確認正式資料仍被忽略後，再推送 GitHub。
